const { getDefaultConfig } = require('expo/metro-config');

const config = getDefaultConfig(__dirname);

config.maxWorkers = 1;
config.stickyWorkers = false;
config.resolver.useWatchman = false;

module.exports = config;
