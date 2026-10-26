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
  config["proxy-groups"] = (config["proxy-groups"] || [])
    .filter(group => !names.includes(group.name))
    .concat(overlay["proxy-groups"]);

  config.rules = Array.from(new Set(overlay.rules.concat(config.rules || [])));
  return config;
}
