#!/usr/bin/env python3
"""从通用配置源生成 Clash Verge Rev 和 OpenClash 的订阅叠加配置。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
DIALER_GROUP = "jump-default"
PROXY_PROVIDER = "local-proxies"
OPENCLASH_DNS_POLICY_KEY = "__overlay-dns-policy"
OPENCLASH_GROUP_NAMES_KEY = "__overlay-group-names"
BUILTINS = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE"}
RULE_OPTIONS = {"no-resolve", "src"}
HEADER = "# 此文件由 scripts/build.py 自动生成，请修改源文件后重新构建。\n"


def load_yaml(path: Path) -> dict:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        # 不将包含节点凭据的 YAML 原文写入终端。
        mark = getattr(error, "problem_mark", None)
        location = f"，第 {mark.line + 1} 行" if mark else ""
        raise ValueError(f"{path.name}: YAML 语法错误{location}") from None
    if not isinstance(value, dict):
        raise ValueError(f"{path.name}: 顶层必须是映射")
    return value


def named_items(value: object, label: str, *, allow_empty: bool = False) -> list[dict]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ValueError(f"{label}: 必须是{'列表' if allow_empty else '非空列表'}")
    names = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"{label}: 第 {index + 1} 项必须是映射")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{label}: 第 {index + 1} 项缺少有效名称")
        if name in names or name in BUILTINS:
            raise ValueError(f"{label}: 名称重复或与内置策略冲突：{name}")
        if not isinstance(item.get("type"), str) or not item["type"]:
            raise ValueError(f"{label}: {name} 缺少 type")
        names.add(name)
    return value


def rule_list(value: object, label: str, targets: set[str], *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ValueError(f"{label}: 必须是{'规则列表' if allow_empty else '非空规则列表'}")
    for index, rule in enumerate(value):
        if not isinstance(rule, str):
            raise ValueError(f"{label}: 第 {index + 1} 项必须是规则字符串")
        parts = [part.strip() for part in rule.split(",")]
        if parts[0].upper() == "MATCH":
            raise ValueError(f"{label}: 叠加配置不能包含 MATCH，兜底规则由原订阅提供")
        minimum_parts = 3
        if len(parts) < minimum_parts:
            raise ValueError(f"{label}: 第 {index + 1} 条规则缺少条件或目标策略")
        # 附加参数位于目标策略之后；不能将末尾的有效策略名误认为
        # 整条规则正确，例如 IP-CIDR,网段,no-resolve,Anthropic。
        arguments = parts.copy()
        while len(arguments) > minimum_parts and arguments[-1] in RULE_OPTIONS:
            arguments.pop()
        if any(part in RULE_OPTIONS for part in arguments[2:]):
            raise ValueError(
                f"{label}: 第 {index + 1} 条规则的 no-resolve/src 必须放在目标策略之后"
            )
        target = arguments[-1]
        if target not in targets:
            raise ValueError(f"{label}: 第 {index + 1} 条规则引用了未定义的目标策略")
    return list(dict.fromkeys(value))


def dns_policy(value: object, label: str) -> dict:
    if not isinstance(value, dict) or set(value) != {"nameserver-policy"}:
        raise ValueError(f"{label}: 只应包含 nameserver-policy，其他 DNS 设置由原订阅提供")
    policy = value["nameserver-policy"]
    if not isinstance(policy, dict) or not policy:
        raise ValueError(f"{label}: nameserver-policy 必须是非空映射")
    for domain, servers in policy.items():
        if not isinstance(domain, str) or not domain.strip():
            raise ValueError(f"{label}: nameserver-policy 必须使用非空域名模式作为键")
        entries = servers if isinstance(servers, list) else [servers]
        if not entries or any(not isinstance(server, str) or not server.strip() for server in entries):
            raise ValueError(f"{label}: nameserver-policy 的解析服务器必须是非空字符串或非空字符串列表")
    return policy


def load_overlay(root: Path) -> dict:
    source = load_yaml(root / "proxies.yaml")
    if set(source) != {"proxies"}:
        raise ValueError("proxies.yaml: 只应包含 proxies")
    proxies = named_items(source["proxies"], "proxies.yaml: proxies")
    paths = sorted(
        path for path in (root / "rules").glob("*")
        if path.is_file() and path.suffix.lower() in {".yaml", ".yml"}
    )
    if not paths:
        raise ValueError("rules/: 至少需要一个 .yaml 或 .yml 规则文件")
    policies = []
    groups = []
    group_sources = {}
    nameserver_policy = {}
    dns_sources = {}
    for path in paths:
        label = path.relative_to(root).as_posix()
        policy = load_yaml(path)
        if not policy or set(policy) - {"proxy-groups", "rules", "dns"}:
            raise ValueError(
                f"{label}: 只应包含标准的 proxy-groups、rules 和 dns，至少提供一项；节点统一在 proxies.yaml 定义"
            )
        if "dns" in policy:
            for domain, servers in dns_policy(policy["dns"], f"{label}: dns").items():
                if domain in dns_sources:
                    raise ValueError(f"{label}: DNS 策略域名重复：{domain}，已在 {dns_sources[domain]} 定义")
                dns_sources[domain] = label
                nameserver_policy[domain] = servers
        file_groups = named_items(policy.get("proxy-groups", []), f"{label}: proxy-groups", allow_empty=True)
        for group in file_groups:
            name = group["name"]
            if name in group_sources:
                raise ValueError(f"{label}: 策略组名称重复：{name}，已在 {group_sources[name]} 定义")
            group_sources[name] = label
            # 未指定候选来源的业务组默认使用唯一的个人节点集合。
            if "use" not in group and "proxies" not in group:
                group = {**group, "use": [PROXY_PROVIDER]}
            groups.append(group)
        policies.append((label, policy))
    if any(group["name"] == DIALER_GROUP for group in groups):
        raise ValueError(f"{DIALER_GROUP} 由构建脚本自动生成，请勿在源文件中重复定义")
    # 按个人节点的完整名称排除，不依赖 Static- 前缀，也不依赖机场分组名。
    # 只转义 RE2 的正则元字符，保留中文、空格等普通字符。
    escaped_names = [
        "".join("\\" + char if char in r"\.^$|?*+()[]{}" else char for char in proxy["name"])
        for proxy in proxies
    ]
    groups = [{
        "name": DIALER_GROUP,
        "type": "select",
        "include-all": True,
        "exclude-filter": "^(?:" + "|".join(escaped_names) + ")$",
        "exclude-type": "direct|reject",
        "empty-fallback": "REJECT",
    }] + groups
    group_names = {group["name"] for group in groups}
    if group_names & {proxy["name"] for proxy in proxies}:
        raise ValueError("节点和策略组不能重名")
    for group in groups:
        uses = group.get("use", [])
        if not isinstance(uses, list) or any(name != PROXY_PROVIDER for name in uses):
            raise ValueError(f"{group['name']}: use 只能引用 {PROXY_PROVIDER}")
        references = group.get("proxies", [])
        if not isinstance(references, list) or any(
            not isinstance(name, str) or name not in group_names | BUILTINS
            for name in references
        ):
            raise ValueError(f"{group['name']}: proxies 应引用已定义的策略组或内置策略；节点默认从 proxies.yaml 引入")
    # 所有策略组加载后再校验引用，允许引用后续文件中定义的组。
    rules = []
    for label, policy in policies:
        rules.extend(rule_list(policy.get("rules", []), f"{label}: rules", group_names | BUILTINS, allow_empty=True))
    rules = list(dict.fromkeys(rules))
    for proxy in proxies:
        if "dialer-proxy" in proxy and (
            not isinstance(proxy["dialer-proxy"], str) or not proxy["dialer-proxy"].strip()
        ):
            raise ValueError(f"{proxy['name']}: dialer-proxy 必须是有效的节点或策略组名称")
    # 默认值仅写入产物；源文件中显式指定的前置保留。
    proxies = [{"dialer-proxy": DIALER_GROUP, **proxy} for proxy in proxies]
    overlay = {
        "profile": {"store-selected": True},
        "proxy-providers": {PROXY_PROVIDER: {"type": "inline", "payload": proxies}},
        "proxy-groups": groups,
        "rules": rules,
    }
    if nameserver_policy:
        overlay["dns"] = {"nameserver-policy": nameserver_policy}
    return overlay


def dump_yaml(value: dict, *, openclash: bool = False) -> str:
    # OpenClash 会展开 YAML 块里的环境变量。双引号字符串里的 Unicode
    # 转义可保留节点凭据和正则中的字面量 $，在 YAML 解析时恢复。
    text = yaml.safe_dump(
        value, allow_unicode=True, sort_keys=False, width=4096,
        default_style='"' if openclash else None,
    )
    return text.replace("$", r"\u0024") if openclash else text


def render(root: Path) -> dict[str, str]:
    overlay = load_overlay(root)

    verge = (root / "clients/clash-verge/extension.js").read_text(encoding="utf-8")
    verge = verge.replace("__PROXY_PROVIDER__", PROXY_PROVIDER).replace("__DIALER_GROUP__", DIALER_GROUP)
    verge = verge.replace("__OVERLAY_CONFIG__", json.dumps(overlay, ensure_ascii=False, indent=2))

    patch = {
        "profile": overlay["profile"],
        # 先删除同名集合再加入；当原配置没有 proxy-providers 时，
        # OpenClash 不会解析新建映射内部的 ! 操作符。
        "proxy-providers-": [PROXY_PROVIDER],
        "proxy-providers": overlay["proxy-providers"],
        "proxy-groups+": overlay["proxy-groups"],
        "+rules": overlay["rules"],
        OPENCLASH_GROUP_NAMES_KEY: [group["name"] for group in overlay["proxy-groups"]],
    }
    dns_overwrite = ""
    if "dns" in overlay:
        # '+' 开头的域名会被 OpenClash 当作合并操作符。先以键值对列表
        # 携带策略，再由 helper 合并并删除临时字段，保留域名原文。
        patch[OPENCLASH_DNS_POLICY_KEY] = list(overlay["dns"]["nameserver-policy"].items())
        dns_overwrite = (
            'ruby_edit "$CONFIG_FILE" \'["dns"]\' '
            '\'(Value["dns"] || {}).merge("nameserver-policy" => '
            '((Value["dns"] || {})["nameserver-policy"] || {}).merge('
            f'Value.delete("{OPENCLASH_DNS_POLICY_KEY}").to_h))\''
        )
    openclash = (root / "clients/openclash/overwrite.conf").read_text(encoding="utf-8")
    openclash = openclash.replace("__OVERLAY_CONFIG__", dump_yaml(patch, openclash=True).rstrip())
    openclash = openclash.replace("__DNS_OVERWRITE__", dns_overwrite)
    group_expression = " ".join(
        line.strip()
        for line in (root / "clients/openclash/jump-groups.rb").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    openclash = openclash.replace("__JUMP_GROUPS__", group_expression)

    return {
        "clash-verge.js": "// 由 scripts/build.py 自动生成，请修改源文件后重新构建。\n" + verge,
        "openclash.conf": HEADER + openclash,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist", help="输出目录，默认 dist")
    args = parser.parse_args()
    try:
        artifacts = render(ROOT)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in artifacts.items():
            path = args.output_dir / filename
            path.write_text(content, encoding="utf-8")
            print(f"已生成：{path}")
    except (OSError, ValueError) as error:
        print(f"构建失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
