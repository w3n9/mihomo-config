// 将个人代理集合与分流规则合并到当前订阅。
function main(config) {
  const overlay = __OVERLAY_CONFIG__;

  config.profile = Object.assign({}, config.profile || {}, overlay.profile);

  if (overlay.dns) {
    const dns = config.dns || {};
    config.dns = Object.assign({}, dns, {
      "nameserver-policy": Object.assign(
        {}, dns["nameserver-policy"] || {}, overlay.dns["nameserver-policy"]
      )
    });
  }

  config["proxy-providers"] = Object.assign(
    {}, config["proxy-providers"] || {}, overlay["proxy-providers"]
  );

  const names = overlay["proxy-groups"].map(group => group.name);
  const seen = new Set();
  config["proxy-groups"] = (config["proxy-groups"] || [])
    .filter(group => !names.includes(group.name))
    .reverse()
    .filter(group => {
      if (seen.has(group.name)) return false;
      seen.add(group.name);
      return true;
    })
    .reverse()
    .concat(overlay["proxy-groups"]);

  // 同时提供订阅策略组；排除会返回前置组或个人落地节点的依赖链。
  const providers = config["proxy-providers"];
  const localNodes = providers["__PROXY_PROVIDER__"].payload;
  const blocked = new Set(names.concat(localNodes.map(proxy => proxy.name)));
  const dependencies = new Map();
  const matchesPattern = (name, pattern, uncertain) => {
    try {
      return Boolean(pattern) && new RegExp(pattern).test(name);
    } catch (_) {
      return uncertain;
    }
  };
  const matches = (proxy, group) => {
    // 无法解释的正则不能用来证明某个落地节点已被排除。
    return (!group.filter || matchesPattern(proxy.name, group.filter, true)) &&
      !matchesPattern(proxy.name, group["exclude-filter"], false) &&
      !(group["exclude-type"] || "").split("|").includes(proxy.type);
  };
  const addNode = (proxy, override = {}) => {
    const dialer = override["dialer-proxy"] || proxy["dialer-proxy"];
    const references = dependencies.get(proxy.name) || [];
    if (dialer) references.push(dialer);
    dependencies.set(proxy.name, references);
  };
  (config.proxies || []).forEach(proxy => addNode(proxy));
  Object.values(providers).forEach(provider => {
    (provider.payload || []).forEach(proxy => addNode(proxy, provider.override));
  });
  config["proxy-groups"].forEach(group => {
    const references = (group.proxies || []).slice();
    if (group["dialer-proxy"]) references.push(group["dialer-proxy"]);
    if (group["empty-fallback"]) references.push(group["empty-fallback"]);
    if (group["include-all"] || group["include-all-proxies"]) {
      (config.proxies || []).filter(proxy => matches(proxy, group))
        .forEach(proxy => references.push(proxy.name));
    }
    const uses = new Set(group.use || []);
    if (group["include-all"] || group["include-all-providers"]) {
      Object.keys(providers).forEach(name => uses.add(name));
    }
    uses.forEach(name => {
      const provider = providers[name] || {};
      const candidates = (provider.payload || [])
        .filter(proxy => matches(proxy, provider) && matches(proxy, group));
      if ((!Array.isArray(provider.payload) || candidates.length) &&
          provider.override && provider.override["dialer-proxy"]) {
        references.push(provider.override["dialer-proxy"]);
      }
      candidates.forEach(proxy => references.push(proxy.name));
    });
    dependencies.set(group.name, references);
  });
  const memo = new Map();
  const unsafe = (name, path = new Set()) => {
    if (blocked.has(name) || path.has(name)) return true;
    if (memo.has(name)) return memo.get(name);
    const next = new Set(path);
    next.add(name);
    const result = (dependencies.get(name) || []).some(target => unsafe(target, next));
    memo.set(name, result);
    return result;
  };
  const candidates = config["proxy-groups"]
    .filter(group => !unsafe(group.name))
    .map(group => group.name);
  const jump = config["proxy-groups"].find(group => group.name === "__DIALER_GROUP__");
  if (candidates.length) jump.proxies = candidates;

  config.rules = Array.from(new Set(overlay.rules.concat(config.rules || [])));
  return config;
}
