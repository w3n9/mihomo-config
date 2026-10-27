# 个人 Mihomo 配置

一份节点列表和多份分流策略，生成 Clash Verge Rev 扩展脚本与 OpenClash 覆写模块，叠加到已有订阅配置上使用。

## 快速开始

需要 Python 3.10+ 和 [uv](https://docs.astral.sh/uv/)。一般使用者直接使用仓库提供的分流规则，只需准备自己的节点配置并构建：

1. 在仓库根目录，将示例复制为本地配置：

   ```sh
   cp proxies.yaml.example proxies.yaml
   ```

2. 编辑 `proxies.yaml`，将示例中的服务器地址、端口、用户名和密码替换为实际节点信息，端口填写整数。可按需增加节点或调整协议类型；沿用默认 Anthropic 规则时，可保留示例节点名称。若修改名称，需确保它符合 `rules/anthropic.yaml` 中的 `filter`。

3. 执行构建：

   ```sh
   uv run scripts/build.py
   ```

`uv` 自动安装锁定的依赖。生成文件位于 `dist/`：

| 文件 | 用途 |
| --- | --- |
| `dist/clash-verge.js` | 已有订阅时，作为 Clash Verge Rev 扩展脚本 |
| `dist/openclash.conf` | 已有订阅时，作为 OpenClash 覆写模块 |

构建完成后，按下方「客户端接入」将对应产物应用到已有订阅。以后修改 `proxies.yaml` 后，重新构建并更新客户端中的产物即可。

也可以指定输出目录：

```sh
uv run scripts/build.py --output-dir /tmp/mihomo-config
```

产物内嵌节点，不需要额外部署订阅服务或复制 provider 文件。本地 `proxies.yaml` 和生成目录 `dist/` 均包含节点凭据，已加入 `.gitignore`；仓库中的 `proxies.yaml.example` 仅作为示例模板。

## 日常维护

```text
proxies.yaml.example            节点示例，首次使用时复制为 proxies.yaml
proxies.yaml                    全部个人节点，顶层为 proxies
rules/*.yaml、rules/*.yml         通用策略，支持 proxy-groups、rules、dns
clients/clash-verge/extension.js Clash Verge Rev 适配模板
clients/openclash/overwrite.conf OpenClash 适配模板
clients/openclash/jump-groups.rb OpenClash 前置策略组收集逻辑
scripts/build.py                 构建与基础校验
dist/                           自动生成的客户端文件
```

所有个人节点统一在根目录的 `proxies.yaml` 中定义，不能放在规则文件中。业务策略组默认从这份节点列表引入节点，无需填写 `use`，只需用 `filter` 筛选。构建脚本会在产物中自动补充对内联代理集合 `local-proxies` 的引用，源文件保持不变。当前 `Anthropic` 组筛选名称符合 `.+static-.+` 的节点。

构建脚本自动导入 `rules/` 目录直属的所有 `.yaml` 和 `.yml` 文件，按文件名排序后依次合并，保留每个文件内部的规则顺序。需要指定优先级时，可以使用 `00-common.yaml`、`10-anthropic.yaml` 等前缀。完全相同的规则只保留第一次出现的位置；不同文件不能定义同名策略组。

每条分流规则需要指定出口，例如：

```yaml
proxy-groups:
  - name: Anthropic
    type: select
    filter: ".+static-.+"

rules:
  - DOMAIN-SUFFIX,claude.ai,Anthropic
  - IP-CIDR,160.79.104.0/21,Anthropic,no-resolve
```

`no-resolve`、`src` 等附加参数放在目标策略之后。构建脚本会拒绝 `IP-CIDR,网段,no-resolve,Anthropic` 这种参数错位的规则。

新增其他业务时，可在 `rules/` 下新建 YAML 文件，也可在已有文件中追加策略组和对应规则。每个文件至少提供 `proxy-groups`、`rules`、`dns` 中的一项，也允许组和规则为空列表。`dns` 仅支持 `nameserver-policy`；不同文件不能重复定义同一个 DNS 策略键。所有文件加载后统一校验组和规则引用，因此策略组和规则可以引用其他文件定义的组，不受文件顺序限制。若策略组显式填写 `proxies` 来引用其他自定义策略组或 `DIRECT` 等内置策略，则仅使用该列表，不再自动加入个人节点。自动生成的 `jump-default` 收集订阅中的前置节点和可用策略组。

规则源文件不使用 `+rules`、`proxy-groups+` 等客户端专用语法，也不放 `MATCH`。`rules/` 中至少需要一个 YAML 规则文件。构建脚本会检查名称重复、provider 引用和规则出口；更完整的协议、正则和组引用环校验由 Mihomo 完成。

## Anthropic DNS

普通分流规则控制网站连接的出口，不会自动改变 DNS 的解析路径。`rules/anthropic.yaml` 为 Anthropic 核心域名、现有规则中的相关服务域名和 DNS 检测域名添加了单独的解析策略，例如：

```yaml
dns:
  nameserver-policy:
    "+.claude.ai": "https://1.1.1.1/dns-query#Anthropic"
    "+.d.ip.net.coffee": "https://1.1.1.1/dns-query#Anthropic"
```

`+.` 匹配域名自身及各级子域名；`#Anthropic` 让这些查询通过 `Anthropic` 当前选择的节点发送到 Cloudflare DoH。请在该组选择美国落地节点。使用 IP 地址作为 DoH 地址，无需先解析 DNS 服务器的域名。其他域名继续使用原订阅的 DNS；原订阅中的 `nameserver`、`fallback`、DNS 监听和启用状态均保留，同名 `nameserver-policy` 键由个人定义替换。

`dns.respect-rules` 控制连接 DNS 服务器的流量是否遵循路由规则，匹配的是 DNS 服务器的地址，不会根据正在查询的业务域名自动选择出口。本配置使用 `nameserver-policy` 按查询域名分流，并通过 `#Anthropic` 指定出口，无需开启全局 `respect-rules`。如果另行启用该选项，应按 Mihomo 文档配置 `proxy-server-nameserver`，避免代理节点域名解析产生循环。

DNS 策略不能直接表达 `DOMAIN-KEYWORD`，因此配置中列出了 Datadog、Sift 的已知服务域名；今后新增此类服务域名时需同步维护 DNS 策略。

[Claude 检测页](https://ip.net.coffee/claude/) 使用 `d.ip.net.coffee` 下的随机域名检查 DNS，并不直接检查 Anthropic 的解析。配置同时为这些探测域名添加了 DNS 策略和流量规则，兼容 fake-ip 模式下 SOCKS5 节点的远端解析。检测站主页及其他域名仍沿用原订阅规则。页面显示的是递归 DNS 服务器的出口归属地，可能与美国代理 IP 不同；Cloudflare 的 Anycast 路由和 IP 地理库也会影响显示结果，不能保证始终标为美国。

重新构建并在客户端替换产物、应用配置后再检测。浏览器和系统的 DNS 查询需要进入 Mihomo；浏览器若启用独立的「安全 DNS」，应关闭它或确认其解析路径经过预期代理。新策略不会使已经缓存的解析结果立即失效，必要时清理缓存或重启客户端。此仓库不自动开启 DNS、TUN 或 DNS 劫持，也不直接修改正在运行的客户端。

## 前置代理

当前静态 SOCKS5 入口拒绝大陆出口 IP 直接接入，需要一条服务商允许的前置链路。可以直接使用已有订阅中的境外节点，路径为：

```text
本机 → 已有订阅的前置节点 → 个人静态节点 → 目标网站
```

构建脚本会自动创建名为 `jump-default` 的 `select` 组，并为没有显式指定 `dialer-proxy` 的个人节点生成：

```yaml
dialer-proxy: jump-default
```

无需在源文件中重复填写默认前置，也无需知道机场的节点组或 provider 名称。生成的前置组使用 `include-all`，由 Mihomo 在加载配置时收集原订阅中的普通节点及代理集合节点。两端适配器还会在合并订阅时收集原有的 `select`、`url-test`、`fallback` 等策略组，作为 `jump-default` 的显式选项；可以选择订阅的自动选择组，也可以继续直接选择节点。

- 构建时根据 `proxies.yaml` 中全部个人节点的完整名称生成排除正则，避免将落地节点选作自身的前置；节点名称中的正则特殊字符会转义。
- 策略组选项按订阅中的顺序保留；同名组以最后定义为准。自动收集的节点继续由 Mihomo 排序。首次使用时默认选中第一项，需要手动确认一个可用的境外节点或策略组。
- 收集策略组时排除本仓库新增或覆盖的业务组、`jump-default` 自身，以及直接或间接依赖这些组、个人落地节点的组。也会检查节点前置、内联代理集合、自动收集节点和筛选条件，避免新增的策略组选项使前置链路形成循环。`direct`、`reject` 类型的普通节点仍会排除。
- 两端产物都会启用 `profile.store-selected`，保存手动选择；原有 `profile` 中其他设置保留。不同订阅若有同名节点，选择记忆也可能复用。
- 原订阅更新或换成另一份订阅时，候选会随最终配置变化。保存的节点名不存在时，Mihomo 会选择当前列表中的第一项，需要重新确认。
- 没有其他节点可用时，`empty-fallback: REJECT` 使该组拒绝连接；这个选项只处理候选列表为空的情况，不会在所有节点超时时自动切换。

个人节点不需要遵守任何命名前缀才能从前置组排除。业务组自身的 `filter` 仍按各规则文件中的配置生效，例如当前 `Anthropic` 组使用 `rules/anthropic.yaml` 中的筛选表达式。

如果某个个人节点确实需要其他前置，可以在该节点的源配置中显式设置 `dialer-proxy`，构建时会保留。显式引用的节点或策略组需要在最终订阅配置中存在。`jump-default` 是自动生成的保留组名，请不要在规则源文件中再定义同名组。

构建并将产物应用到订阅后，先在 `jump-default` 中选择可用的境外节点，再测试个人静态节点。前置节点自身的测速结果只反映它访问测速站的情况；静态节点的测试结果才能验证整条代理链。

## 客户端接入

### Clash Verge Rev

1. 构建后打开 `dist/clash-verge.js`，复制全部内容。
2. 在「订阅」页面右键目标订阅，进入该订阅的扩展脚本编辑器（通常为「编辑脚本」）。
3. 粘贴生成脚本并保存，应用或重新激活该订阅。
4. 在代理面板中的 `jump-default` 选择可用的订阅节点或策略组，再在 `Anthropic` 选择你的落地节点。

如果希望所有订阅都应用，可将同一脚本放入全局扩展脚本。一个订阅选择一种挂载方式即可；已有其他扩展脚本时，需要将其逻辑与此脚本合并，保持一个 `main(config)` 入口。

脚本操作代理集合、策略组、规则和按域名指定的 DNS 策略，并开启策略选择记忆。原订阅的节点、其他集合、其他策略组及其他设置保留。规则顺序为「个人规则 → 原订阅规则」，最终兜底仍由原订阅决定。

### OpenClash

需要支持 `[YAML]` 和 `[Overwrite]` 的覆写模块版本，使用 Mihomo / Meta 内核。

1. 将生成的 `dist/openclash.conf` 作为本地覆写模块导入；若界面需要先创建本地模块，则将文件内容粘贴到模块编辑器中。
2. 启用该模块，并在模块的配置匹配设置中选择需要应用的订阅配置。
3. 保存并重启 OpenClash，先在 `jump-default` 选择可用的订阅节点或策略组，再在 `Anthropic` 选择落地节点。

使用的是「覆写模块」功能，不是把 `.conf` 当作完整 YAML 配置上传。模块里的 YAML 块追加策略组、前置规则；helper 在 YAML 合并后按名称去重策略组，以最后追加的个人定义为准。

如果另外启用了会重建策略组或替换规则的功能，需要检查最终运行配置，确认个人规则仍在原订阅规则前面。

## 合并约定

- `local-proxies` 是本仓库保留的代理集合名，同名集合会被个人定义完整替换。
- `jump-default` 是自动生成的保留策略组，同名订阅组会被个人定义替换；候选自动收集订阅节点和不会形成循环的订阅策略组。
- 同名策略组以个人定义为准，不保留旧组的测速 URL 等字段；其他策略组保留。
- 个人规则前置，完全相同的规则去重；重复应用不会累积重复组或规则。
- 叠加产物不注入 `MATCH`；DNS 仅合并 `nameserver-policy`，其他 DNS 设置、端口和 TUN 保留。

如果原订阅也有 `Anthropic` 组，其引用会指向你的个人定义。需要避免这种覆盖时，可以在源文件里使用 `Custom-Anthropic` 等名称，同时修改规则出口和 DNS 地址中的策略组名称。

## 验证

运行构建和测试：

```sh
uv run scripts/build.py
uv run python -m unittest discover -s tests -v
```

测试使用虚构节点，覆盖多文件合并顺序、跨文件引用、重复组名检查和规则去重；安装 Node.js 后会实际执行 Clash Verge 扩展，验证订阅保留、规则顺序及重复应用。

提供 `MIHOMO_BIN` 后，还会启动临时隔离内核，检查前置组的候选节点、空组拒绝和选择记忆。这个实例关闭代理监听端口、DNS 和 TUN，只使用临时 Unix 控制 socket，不操作正在运行的客户端：

```sh
MIHOMO_BIN=/path/to/mihomo uv run python -m unittest discover -s tests -v
```

OpenClash 集成测试可以直接使用官方仓库中的 `YAML.rb`，验证真实覆写逻辑以及凭据中的 `$`、引号、反斜杠和中文组名：

```sh
OPENCLASH_YAML_LIBRARY=/path/to/OpenClash/luci-app-openclash/root/usr/share/openclash/YAML.rb \
  uv run python -m unittest discover -s tests -v
```

未提供该环境变量时跳过 OpenClash 集成测试。使用支持 `RubyVM::AbstractSyntaxTree` 的 Ruby 执行此项测试。

产物需要先与订阅合并，再由 Mihomo 校验最终配置。构建脚本只写入仓库输出目录，不会自动修改或重载客户端。

## 官方资料

- [Mihomo 代理集合](https://wiki.metacubex.one/config/proxy-providers/)
- [Mihomo 代理组](https://wiki.metacubex.one/config/proxy-groups/)
- [Mihomo 前置代理](https://wiki.metacubex.one/config/proxies/dialer-proxy/)
- [Mihomo 路由规则](https://wiki.metacubex.one/config/rules/)
- [Mihomo DNS 策略与指定代理](https://wiki.metacubex.one/config/dns/)
- [Clash Verge Rev 扩展配置](https://www.clashverge.dev/guide/extend.html)
- [Clash Verge Rev 扩展脚本](https://www.clashverge.dev/guide/script.html)
- [OpenClash 覆写模块示例](https://github.com/vernesong/OpenClash/blob/master/luci-app-openclash/root/etc/openclash/overwrite/default)
