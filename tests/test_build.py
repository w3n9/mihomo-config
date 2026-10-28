"""验证生成配置及真实客户端合并逻辑，使用虚构节点避免读取个人凭据。"""

import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.parse

import yaml

from scripts.build import DIALER_GROUP, ROOT, load_overlay, render


def merge_verge(script, original):
    runner = r"""
const fs = require('fs');
const vm = require('vm');
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
let config = JSON.parse(fs.readFileSync(0, 'utf8'));
const once = JSON.stringify(context.main(config));
const twice = JSON.stringify(context.main(config));
if (once !== twice) throw new Error('重复应用改变了结果');
process.stdout.write(twice);
"""
    result = subprocess.run(
        ["node", "-e", runner, str(script)], input=json.dumps(original),
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def merge_openclash(root, module, original):
    sections = {"YAML": [], "Overwrite": []}
    section = None
    for line in module.splitlines():
        if line in ("[YAML]", "[Overwrite]"):
            section = line[1:-1]
        elif section and line.strip() and not line.lstrip().startswith("#"):
            sections[section].append(line)
    patch = root / "patch.yaml"
    # 与 OpenClash shell 提取阶段一致；生成内容已转义字面量 $。
    if "$" in "\n".join(sections["YAML"]):
        raise AssertionError("YAML 块中存在会被 shell 展开的字面量 $")
    patch.write_text("\n".join(sections["YAML"]), encoding="utf-8")
    manifest = root / "manifest"
    manifest.write_text(
        "M overlay\n" + f"Y {patch}\n" +
        "".join(f"L {line}\n" for line in sections["Overwrite"]), encoding="utf-8",
    )
    runner = r"""
require 'yaml'
require ARGV.shift
YAML.overwrite_run(ARGV[0], ARGV[1])
"""
    config = root / "input.yaml"
    config.write_text(yaml.safe_dump(original, allow_unicode=True), encoding="utf-8")
    once = None
    for _ in range(2):
        result = subprocess.run(
            ["ruby", "-E", "UTF-8", "-e", runner,
             os.environ["OPENCLASH_YAML_LIBRARY"], str(config), str(manifest)],
            capture_output=True, text=True, check=True,
        )
        if "[Error]" in result.stdout or "[Warning]" in result.stdout:
            raise AssertionError(result.stdout)
        merged = yaml.safe_load(config.read_text(encoding="utf-8"))
        if once is not None and merged != once:
            raise AssertionError("重复应用改变了结果")
        once = merged
    return merged


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ("rules", "clients"):
            (self.root / directory).mkdir()
        shutil.copytree(ROOT / "clients", self.root / "clients", dirs_exist_ok=True)
        self.nodes = {"proxies": [{
            "name": "Static-Test", "type": "socks5", "server": "127.0.0.1", "port": 1080,
            "username": "$TEST_TOKEN", "password": "p$a'ss\\word#{literal}",
        }]}
        self.policy = {
            "proxy-groups": [{
                "name": "Anthropic", "type": "select",
                "filter": "^Static-.+$",
            }],
            "rules": ["DOMAIN-SUFFIX,claude.ai,Anthropic"],
        }
        self.write_sources()

    def write_sources(self):
        for name, value in (
            ("proxies.yaml", self.nodes),
            ("rules/anthropic.yaml", self.policy),
        ):
            self.write_yaml(name, value)

    def write_yaml(self, name, value):
        (self.root / name).write_text(yaml.safe_dump(value, allow_unicode=True), encoding="utf-8")

    def subscription(self):
        return {
            "mixed-port": 8888,
            "dns": {"enable": True, "nameserver": ["1.1.1.1"]},
            "tun": {"enable": True},
            "profile": {"store-selected": False, "store-fake-ip": True},
            "proxies": [{"name": "Airport-Node", "type": "socks5", "server": "127.0.0.1", "port": 2080}],
            "proxy-providers": {
                "airport": {"type": "inline", "payload": [{
                    "name": "Provider-Node", "type": "socks5", "server": "127.0.0.1", "port": 2081,
                }]},
                "local-proxies": {"type": "http", "url": "https://example.com/old", "filter": "old"},
            },
            "proxy-groups": [
                {"name": "Airport", "type": "select", "proxies": ["Airport-Node"]},
                {"name": DIALER_GROUP, "type": "select", "proxies": ["DIRECT"]},
                {"name": self.policy["proxy-groups"][0]["name"], "type": "url-test", "url": "https://example.com", "proxies": ["DIRECT"]},
            ],
            "rules": ["DOMAIN,example.com,Airport", self.policy["rules"][0], "MATCH,Airport"],
        }

    def assert_merged(self, merged, original):
        overlay = load_overlay(self.root)
        overlay["proxy-groups"][0]["proxies"] = ["Airport"]
        for key in ("mixed-port", "dns", "tun", "proxies"):
            self.assertEqual(merged[key], original[key])
        self.assertEqual(merged["proxy-providers"]["airport"], original["proxy-providers"]["airport"])
        self.assertEqual(merged["proxy-providers"]["local-proxies"], overlay["proxy-providers"]["local-proxies"])
        self.assertEqual(merged["proxy-groups"], [original["proxy-groups"][0]] + overlay["proxy-groups"])
        self.assertEqual(merged["rules"], overlay["rules"] + ["DOMAIN,example.com,Airport", "MATCH,Airport"])
        self.assertEqual(merged["profile"], {"store-selected": True, "store-fake-ip": True})

    def test_only_renders_client_overlays(self):
        artifacts = render(self.root)
        self.assertEqual(set(artifacts), {"clash-verge.js", "openclash.conf"})
        patch_text = artifacts["openclash.conf"].split("[YAML]\n", 1)[1].split("[Overwrite]", 1)[0]
        patch = yaml.safe_load(patch_text)
        self.assertEqual(patch["proxy-providers"]["local-proxies"]["payload"], self.nodes["proxies"])
        self.assertEqual(patch["+rules"], self.policy["rules"])
        self.assertEqual(patch["proxy-groups+"][1]["use"], ["local-proxies"])
        self.assertFalse({"mixed-port", "dns", "tun", "mode", "rules"} & patch.keys())

    def test_defaults_group_source_without_changing_rule_files(self):
        source = self.root / "rules/anthropic.yaml"
        before = source.read_bytes()
        overlay = load_overlay(self.root)
        self.assertEqual(overlay["proxy-groups"][1], {
            "name": "Anthropic", "type": "select", "filter": "^Static-.+$", "use": ["local-proxies"],
        })
        render(self.root)
        self.assertEqual(source.read_bytes(), before)

    def test_merges_rule_files_in_filename_order_with_cross_file_references(self):
        first_rule = "DOMAIN,first.example,Shared"
        last_rule = "DOMAIN,last.example,Service"
        shared = {"name": "Shared", "type": "select"}
        service = {"name": "Service", "type": "select", "proxies": ["Shared"]}
        # 故意按相反顺序创建，验证顺序取决于文件名而非创建时间。
        self.write_yaml("rules/zz-rules.yaml", {"rules": [last_rule, self.policy["rules"][0]]})
        self.write_yaml("rules/z-groups.yml", {"proxy-groups": [shared]})
        self.write_yaml("rules/00-routing.yaml", {
            "proxy-groups": [service],
            "rules": [first_rule, self.policy["rules"][0]],
        })
        (self.root / "rules/README.md").write_text("规则说明", encoding="utf-8")
        (self.root / "rules/ignored.yaml").mkdir()
        before = {path: path.read_bytes() for path in (self.root / "rules").iterdir() if path.is_file()}

        overlay = load_overlay(self.root)
        self.assertEqual(overlay["proxy-groups"][1:], [
            service,
            {**self.policy["proxy-groups"][0], "use": ["local-proxies"]},
            {**shared, "use": ["local-proxies"]},
        ])
        self.assertEqual(overlay["rules"], [first_rule, *self.policy["rules"], last_rule])
        artifacts = render(self.root)
        patch_text = artifacts["openclash.conf"].split("[YAML]\n", 1)[1].split("[Overwrite]", 1)[0]
        patch = yaml.safe_load(patch_text)
        self.assertEqual(patch["proxy-groups+"], overlay["proxy-groups"])
        self.assertEqual(patch["+rules"], overlay["rules"])
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_rejects_duplicate_group_names_across_rule_files(self):
        self.write_yaml("rules/z-other.yml", {"proxy-groups": self.policy["proxy-groups"]})
        with self.assertRaisesRegex(ValueError, "rules/z-other.yml: 策略组名称重复.*rules/anthropic.yaml"):
            render(self.root)

    def test_merges_dns_policies_from_dns_only_files_without_changing_sources(self):
        endpoint = "https://1.1.1.1/dns-query#Anthropic"
        self.policy["dns"] = {"nameserver-policy": {"+.claude.ai": [endpoint]}}
        self.write_sources()
        extra = {"+.d.ip.net.coffee": endpoint}
        self.write_yaml("rules/z-dns.yml", {"dns": {"nameserver-policy": extra}})
        before = {path: path.read_bytes() for path in (self.root / "rules").iterdir()}

        overlay = load_overlay(self.root)
        self.assertEqual(overlay["dns"], {
            "nameserver-policy": {**self.policy["dns"]["nameserver-policy"], **extra},
        })
        self.assertEqual(overlay["rules"], self.policy["rules"])
        render(self.root)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_rejects_duplicate_dns_policy_keys_with_source_location(self):
        dns = {"nameserver-policy": {"+.claude.ai": "https://1.1.1.1/dns-query#Anthropic"}}
        self.policy["dns"] = dns
        self.write_sources()
        self.write_yaml("rules/z-dns.yml", {"dns": dns})
        with self.assertRaisesRegex(ValueError, r"rules/z-dns.yml:.*rules/anthropic.yaml"):
            render(self.root)

    def test_rejects_invalid_dns_policies_with_source_location(self):
        invalid_dns = [
            None, [], {}, {"enable": True}, {"nameserver-policy": None},
            {"nameserver-policy": []}, {"nameserver-policy": {}},
        ]
        for invalid_value in (None, "", " ", [], [""], [" "], [1], 1, {"server": "1.1.1.1"}):
            invalid_dns.append({"nameserver-policy": {"+.claude.ai": invalid_value}})
        for invalid_key in ("", " ", 1):
            invalid_dns.append({"nameserver-policy": {invalid_key: "1.1.1.1"}})
        for dns in invalid_dns:
            with self.subTest(dns=dns):
                self.write_yaml("rules/z-dns.yml", {"dns": dns})
                with self.assertRaisesRegex(ValueError, r"rules/z-dns.yml:.*dns"):
                    render(self.root)

    def test_rejects_nodes_in_rule_files(self):
        self.write_yaml("rules/z-nodes.yaml", self.nodes)
        with self.assertRaisesRegex(ValueError, "rules/z-nodes.yaml:.*节点统一在 proxies.yaml 定义"):
            render(self.root)

    def test_rejects_missing_rule_files(self):
        (self.root / "rules/anthropic.yaml").unlink()
        (self.root / "rules/README.md").write_text("规则说明", encoding="utf-8")
        (self.root / "rules/ignored.yaml").mkdir()
        with self.assertRaisesRegex(ValueError, "rules/: 至少需要一个"):
            render(self.root)

    def test_rejects_invalid_rule_file_sections_with_source_location(self):
        for policy in ({}, {"proxy-groups": None}, {"rules": "invalid"}, {"rules": ["DOMAIN,example.com,Unknown"]}):
            with self.subTest(policy=policy):
                self.write_yaml("rules/invalid.yml", policy)
                with self.assertRaisesRegex(ValueError, "rules/invalid.yml:"):
                    render(self.root)

    def test_generates_jump_group_without_injecting_node_dialer(self):
        source = self.root / "proxies.yaml"
        before = source.read_bytes()
        overlay = load_overlay(self.root)
        front = overlay["proxy-groups"][0]
        self.assertEqual(front["name"], "jump-default")
        self.assertEqual(front["type"], "select")
        self.assertTrue(front["include-all"])
        self.assertEqual(front["empty-fallback"], "REJECT")
        self.assertNotIn("proxies", front)
        self.assertNotIn("use", front)
        self.assertEqual(overlay["profile"], {"store-selected": True})
        self.assertEqual(overlay["proxy-providers"]["local-proxies"]["payload"], self.nodes["proxies"])
        self.assertNotIn("dialer-proxy", overlay["proxy-providers"]["local-proxies"]["payload"][0])
        render(self.root)
        self.assertEqual(source.read_bytes(), before)

    def test_excludes_exact_local_names_without_naming_convention(self):
        names = ['测试落地 🧪 [1].*+$|', 'another "node"\\name']
        self.nodes["proxies"] = [{**self.nodes["proxies"][0], "name": name} for name in names]
        self.write_sources()
        front = load_overlay(self.root)["proxy-groups"][0]
        pattern = re.compile(front["exclude-filter"])
        for name in names:
            self.assertIsNotNone(pattern.fullmatch(name))
            self.assertIsNone(pattern.fullmatch(name + "-机场"))
        self.assertIsNone(pattern.fullmatch("Airport-Node"))

    def test_preserves_explicit_dialer(self):
        base = self.nodes["proxies"][0]
        self.nodes["proxies"] = [
            dict(base),
            *({**base, "name": f"Static-{index}", "dialer-proxy": dialer}
              for index, dialer in enumerate((DIALER_GROUP, "Custom-Front", "Front-Node", "DIRECT"))),
        ]
        self.write_sources()
        source = self.root / "proxies.yaml"
        before = source.read_bytes()
        self.assertEqual(load_overlay(self.root)["proxy-providers"]["local-proxies"]["payload"], self.nodes["proxies"])
        artifacts = render(self.root)
        patch_text = artifacts["openclash.conf"].split("[YAML]\n", 1)[1].split("[Overwrite]", 1)[0]
        self.assertEqual(yaml.safe_load(patch_text)["proxy-providers"]["local-proxies"]["payload"], self.nodes["proxies"])
        if shutil.which("node"):
            script = self.root / "extension.js"
            script.write_text(artifacts["clash-verge.js"], encoding="utf-8")
            merged = merge_verge(script, self.subscription())
            self.assertEqual(merged["proxy-providers"]["local-proxies"]["payload"], self.nodes["proxies"])
        self.assertEqual(source.read_bytes(), before)

    def test_rejects_invalid_or_duplicate_dialer(self):
        self.nodes["proxies"][0]["dialer-proxy"] = None
        self.write_sources()
        with self.assertRaisesRegex(ValueError, "dialer-proxy"):
            render(self.root)
        del self.nodes["proxies"][0]["dialer-proxy"]
        self.policy["proxy-groups"].append({"name": DIALER_GROUP, "type": "select"})
        self.write_sources()
        with self.assertRaisesRegex(ValueError, "自动生成"):
            render(self.root)

    def test_rejects_missing_policy_and_overlay_match(self):
        for rule in ("DOMAIN-SUFFIX,claude.ai", "DOMAIN-SUFFIX,claude.ai,Unknown", "MATCH,DIRECT"):
            with self.subTest(rule=rule):
                self.policy["rules"] = [rule]
                self.write_sources()
                with self.assertRaises(ValueError):
                    render(self.root)

    def test_rejects_duplicate_names_and_unknown_provider(self):
        self.nodes["proxies"].append(dict(self.nodes["proxies"][0]))
        self.write_sources()
        with self.assertRaisesRegex(ValueError, "名称重复"):
            render(self.root)
        self.nodes["proxies"].pop()
        self.policy["proxy-groups"][0]["use"] = ["missing"]
        self.write_sources()
        with self.assertRaisesRegex(ValueError, "use"):
            render(self.root)

    def test_rejects_rule_options_before_policy(self):
        for rule in (
            "IP-CIDR,160.79.104.0/21,no-resolve,Anthropic",
            "IP-CIDR6,2607:6bc0::/32,no-resolve,Anthropic",
            "IP-ASN,399358,no-resolve,Anthropic",
            "IP-CIDR,160.79.104.0/21,src,Anthropic,no-resolve",
        ):
            with self.subTest(rule=rule):
                self.policy["rules"] = [rule]
                self.write_sources()
                with self.assertRaisesRegex(ValueError, "必须放在目标策略之后"):
                    render(self.root)

    def test_accepts_rule_options_after_policy(self):
        self.policy["rules"] = [
            "IP-CIDR,160.79.104.0/21,Anthropic,no-resolve",
            "IP-CIDR6,2607:6bc0::/32,Anthropic,no-resolve",
            "IP-ASN,399358,Anthropic,no-resolve",
            "IP-CIDR,160.79.104.0/21,Anthropic,src,no-resolve",
            "IP-CIDR,160.79.104.0/21,Anthropic,no-resolve,src",
        ]
        self.write_sources()
        render(self.root)
        self.assertEqual(load_overlay(self.root)["rules"], self.policy["rules"])

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行 Clash Verge 扩展")
    def test_verge_preserves_subscription_and_is_idempotent(self):
        self.write_yaml("rules/extra.yml", {
            "proxy-groups": [{"name": "Extra", "type": "select", "proxies": ["Anthropic"]}],
            "rules": ["DOMAIN,extra.example,Extra"],
        })
        script = self.root / "extension.js"
        script.write_text(render(self.root)["clash-verge.js"], encoding="utf-8")
        for original in (self.subscription(), {}):
            with self.subTest(empty=not original):
                merged = merge_verge(script, original)
                if original:
                    self.assert_merged(merged, original)
                else:
                    self.assertEqual(merged, load_overlay(self.root))

    def subscription_group_variants(self):
        safe = self.subscription()
        safe["proxy-groups"] = [
            {"name": "Airport", "type": "select", "proxies": ["Airport-Node"]},
            {"name": "Duplicate", "type": "select", "proxies": [DIALER_GROUP]},
            {"name": "Auto", "type": "url-test", "use": ["airport"], "url": "https://example.com", "interval": 300},
            {"name": "Backup", "type": "fallback", "proxies": ["Auto"], "url": "https://example.com", "interval": 300},
            {"name": "Duplicate", "type": "select", "proxies": ["DIRECT"]},
            {"name": "Preferred", "type": "select", "proxies": ["Backup", "Airport"]},
            {"name": "AllRaw", "type": "select", "include-all-proxies": True},
            {"name": DIALER_GROUP, "type": "select", "proxies": ["DIRECT"]},
            {"name": "Anthropic", "type": "select", "proxies": ["DIRECT"]},
        ]
        unsafe = self.subscription()
        unsafe["proxies"].append({
            "name": "Chained-Node", "type": "socks5", "server": "127.0.0.1", "port": 2082,
            "dialer-proxy": "Anthropic",
        })
        unsafe["proxy-providers"]["chained"] = {
            "type": "inline", "payload": [{
                "name": "Chained-Provider-Node", "type": "socks5", "server": "127.0.0.1", "port": 2083,
            }], "override": {"dialer-proxy": DIALER_GROUP},
        }
        unsafe["proxy-groups"] = [
            {"name": "Airport", "type": "select", "proxies": ["Airport-Node"]},
            {"name": "Jump", "type": "select", "proxies": [DIALER_GROUP]},
            {"name": "IndirectJump", "type": "select", "proxies": ["Jump"]},
            {"name": "Business", "type": "select", "proxies": ["Anthropic"]},
            {"name": "IndirectBusiness", "type": "select", "proxies": ["Business"]},
            {"name": "Landing", "type": "select", "proxies": ["Static-Test"]},
            {"name": "LocalProvider", "type": "select", "use": ["local-proxies"]},
            {"name": "IndirectLocalProvider", "type": "select", "proxies": ["LocalProvider"]},
            {"name": "ChainedRaw", "type": "select", "proxies": ["Chained-Node"]},
            {"name": "ChainedProvider", "type": "select", "use": ["chained"]},
            {"name": "ChainedProviderNode", "type": "select", "proxies": ["Chained-Provider-Node"]},
            {"name": "SelfLoop", "type": "select", "proxies": ["SelfLoop"]},
            {"name": "EveryNode", "type": "select", "include-all": True},
            {"name": "EveryProvider", "type": "select", "include-all-providers": True},
            {"name": "FilteredNodes", "type": "select", "include-all": True,
             "exclude-filter": "^(Static-Test|Chained-Node|Chained-Provider-Node)$"},
            {"name": DIALER_GROUP, "type": "select", "proxies": ["DIRECT"]},
            {"name": "Anthropic", "type": "select", "proxies": ["DIRECT"]},
        ]
        changed = {"proxy-groups": [{"name": "Changed", "type": "select", "proxies": ["DIRECT"]}]}
        return (
            (safe, ["Airport", "Auto", "Backup", "Duplicate", "Preferred", "AllRaw"]),
            (unsafe, ["Airport", "FilteredNodes"]),
            (changed, ["Changed"]),
            ({}, []),
        )

    def assert_subscription_groups_collected(self, merged, original, expected_names):
        overlay = load_overlay(self.root)
        expected_jump = dict(overlay["proxy-groups"][0])
        if expected_names:
            expected_jump["proxies"] = expected_names
        groups = merged["proxy-groups"]
        self.assertEqual(next(group for group in groups if group["name"] == DIALER_GROUP), expected_jump)
        self.assertEqual(len(groups), len({group["name"] for group in groups}))
        self.assertEqual(set(merged), set(original) | set(overlay))
        if "proxies" in original:
            self.assertEqual(merged["proxies"], original["proxies"])
        for name, provider in original.get("proxy-providers", {}).items():
            if name != "local-proxies":
                self.assertEqual(merged["proxy-providers"][name], provider)
        if "Duplicate" in expected_names:
            self.assertEqual(next(group for group in groups if group["name"] == "Duplicate"), {
                "name": "Duplicate", "type": "select", "proxies": ["DIRECT"],
            })

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行 Clash Verge 扩展")
    def test_verge_collects_safe_subscription_groups_and_retains_nodes(self):
        script = self.root / "extension.js"
        script.write_text(render(self.root)["clash-verge.js"], encoding="utf-8")
        for original, expected in self.subscription_group_variants():
            with self.subTest(expected=expected):
                merged = merge_verge(script, original)
                self.assert_subscription_groups_collected(merged, original, expected)

    @unittest.skipUnless(
        shutil.which("ruby") and os.environ.get("OPENCLASH_YAML_LIBRARY"),
        "设置 OPENCLASH_YAML_LIBRARY 指向官方 YAML.rb 可运行 OpenClash 集成验证",
    )
    def test_openclash_collects_safe_subscription_groups_and_retains_nodes(self):
        module = render(self.root)["openclash.conf"]
        for original, expected in self.subscription_group_variants():
            with self.subTest(expected=expected):
                merged = merge_openclash(self.root, module, original)
                self.assert_subscription_groups_collected(merged, original, expected)

    def dns_subscription_variants(self):
        original = self.subscription()
        original["dns"].update({
            "enable": False,
            "respect-rules": False,
            "proxy-server-nameserver": ["223.5.5.5"],
            "fallback": ["8.8.8.8"],
            "nameserver-policy": {
                "+.claude.ai": "223.5.5.5",
                "+.d.ip.net.coffee": ["114.114.114.114", "223.5.5.5"],
                "+.unrelated.example": ["192.0.2.53"],
            },
        })
        list_conflict = self.subscription()
        list_conflict["dns"]["nameserver-policy"] = {"+.claude.ai": ["223.5.5.5"]}
        return (
            original, list_conflict, self.subscription(),
            {"dns": {"enable": True, "nameserver-policy": None}},
            {"dns": None}, {},
        )

    def assert_dns_merged(self, merged, original):
        overlay = load_overlay(self.root)
        expected_dns = dict(original.get("dns") or {})
        expected_dns["nameserver-policy"] = {
            **(expected_dns.get("nameserver-policy") or {}),
            **overlay["dns"]["nameserver-policy"],
        }
        self.assertEqual(merged["dns"], expected_dns)
        self.assertEqual(set(merged), set(original) | set(overlay))
        for key in ("mixed-port", "tun", "proxies"):
            if key in original:
                self.assertEqual(merged[key], original[key])
        if not original:
            self.assertEqual(merged, overlay)

    def use_dns_policy(self, group_name="Anthropic"):
        self.policy["proxy-groups"][0]["name"] = group_name
        self.policy["rules"] = [f"DOMAIN-SUFFIX,claude.ai,{group_name}"]
        endpoint = f"https://1.1.1.1/dns-query#{group_name}"
        self.policy["dns"] = {"nameserver-policy": {
            "+.claude.ai": [endpoint],
            "+.d.ip.net.coffee": endpoint,
        }}
        self.write_sources()

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行 Clash Verge 扩展")
    def test_verge_merges_only_target_dns_policies_and_is_idempotent(self):
        self.use_dns_policy()
        script = self.root / "extension.js"
        script.write_text(render(self.root)["clash-verge.js"], encoding="utf-8")
        for original in self.dns_subscription_variants():
            with self.subTest(original=original):
                self.assert_dns_merged(merge_verge(script, original), original)

    @unittest.skipUnless(
        shutil.which("ruby") and os.environ.get("OPENCLASH_YAML_LIBRARY"),
        "设置 OPENCLASH_YAML_LIBRARY 指向官方 YAML.rb 可运行 OpenClash 集成验证",
    )
    def test_openclash_merges_literal_dns_wildcards_and_is_idempotent(self):
        for group_name in ("Anthropic", '测试 🧪 "AI" #{$TEST_TOKEN}\'s'):
            self.use_dns_policy(group_name)
            module = render(self.root)["openclash.conf"]
            for original in self.dns_subscription_variants():
                with self.subTest(group_name=group_name, original=original):
                    self.assert_dns_merged(merge_openclash(self.root, module, original), original)

    @unittest.skipUnless(
        shutil.which("node") and os.environ.get("MIHOMO_BIN"),
        "设置 MIHOMO_BIN 可运行临时隔离内核，验证候选节点与选择记忆",
    )
    def test_mihomo_candidates_and_selection_memory(self):
        # 所有节点均为虚构地址；关闭代理端口、DNS 和 TUN，仅通过临时
        # Unix socket 检查代理组。不会修改或连接用户正在运行的内核。
        local_name = '测试落地 🧪 [1].*+$|'
        self.nodes["proxies"][0]["name"] = local_name
        self.write_sources()
        script = self.root / "extension.js"
        script.write_text(render(self.root)["clash-verge.js"], encoding="utf-8")
        merged = merge_verge(script, self.subscription())
        directory = self.root / "isolated-core"
        directory.mkdir()
        control = directory / "control.sock"
        merged.update({
            "mixed-port": 0, "port": 0, "socks-port": 0, "redir-port": 0, "tproxy-port": 0,
            "mode": "rule", "log-level": "error", "dns": {"enable": False},
            "tun": {"enable": False}, "external-controller-unix": str(control),
        })
        config = directory / "config.yaml"
        config.write_text(yaml.safe_dump(merged, allow_unicode=True), encoding="utf-8")

        class UnixConnection(http.client.HTTPConnection):
            def connect(self):
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(1)
                self.sock.connect(str(control))

        endpoint = "/proxies/" + urllib.parse.quote(DIALER_GROUP, safe="")

        def api(method, body=None):
            connection = UnixConnection("localhost", timeout=1)
            try:
                connection.request(
                    method, endpoint, body=json.dumps(body) if body else None,
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                content = response.read()
                self.assertLess(response.status, 300, content.decode())
                return json.loads(content) if content else None
            finally:
                connection.close()

        def check_core(check):
            log_path = directory / "core.log"
            with log_path.open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [os.environ["MIHOMO_BIN"], "-d", str(directory), "-f", str(config)],
                    stdout=log, stderr=subprocess.STDOUT,
                )
                try:
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            state = api("GET")
                            break
                        except (OSError, http.client.HTTPException):
                            if process.poll() is not None or time.monotonic() >= deadline:
                                self.fail(log_path.read_text(encoding="utf-8"))
                            time.sleep(0.05)
                    check(state)
                finally:
                    process.terminate()
                    process.wait(timeout=5)

        def initial(state):
            self.assertEqual(set(state["all"]), {"Airport", "Airport-Node", "Provider-Node"})
            self.assertNotIn(local_name, state["all"])
            api("PUT", {"name": "Provider-Node"})

        check_core(initial)
        check_core(lambda state: self.assertEqual(state["now"], "Provider-Node"))

        variants = (
            ({"proxies": self.subscription()["proxies"]}, ["Airport-Node"]),
            ({"proxy-providers": {"另一个机场": self.subscription()["proxy-providers"]["airport"]}}, ["Provider-Node"]),
            ({}, ["REJECT"]),
        )
        for original, expected in variants:
            with self.subTest(expected=expected):
                overlay = merge_verge(script, original)
                merged["proxies"] = overlay.get("proxies", [])
                merged["proxy-providers"] = overlay["proxy-providers"]
                merged["proxy-groups"] = overlay["proxy-groups"]
                merged["rules"] = overlay["rules"] + ["MATCH,DIRECT"]
                config.write_text(yaml.safe_dump(merged, allow_unicode=True), encoding="utf-8")
                check_core(lambda state: self.assertEqual(state["all"], expected))

    @unittest.skipUnless(
        shutil.which("ruby") and os.environ.get("OPENCLASH_YAML_LIBRARY"),
        "设置 OPENCLASH_YAML_LIBRARY 指向官方 YAML.rb 可运行 OpenClash 集成验证",
    )
    def test_openclash_official_parser_and_special_characters(self):
        for name in ("Anthropic", '测试 🧪 "AI" #{$TEST_TOKEN}\'s'):
            with self.subTest(name=name):
                self.policy["proxy-groups"][0]["name"] = name
                self.policy["rules"] = [f"DOMAIN-SUFFIX,claude.ai,{name}"]
                self.write_sources()
                module = render(self.root)["openclash.conf"]
                flat_subscription = self.subscription()
                del flat_subscription["proxy-providers"]
                for original in (self.subscription(), flat_subscription, {}):
                    merged = merge_openclash(self.root, module, original)
                    if "proxy-providers" in original:
                        self.assert_merged(merged, original)
                    elif original:
                        self.assertEqual(merged["proxies"], original["proxies"])
                        self.assertEqual(merged["proxy-providers"], load_overlay(self.root)["proxy-providers"])
                        self.assertEqual(merged["rules"][-1], "MATCH,Airport")
                    else:
                        self.assertEqual(merged, load_overlay(self.root))


if __name__ == "__main__":
    unittest.main()
