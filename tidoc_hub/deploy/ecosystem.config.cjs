module.exports = {
  apps: [
    {
      name: 'bitfsae-tidoc-hub',
      cwd: '/opt/bitfsae-tidoc-hub/current',
      script: 'python3',
      args: ['-m', 'tidoc_hub', 'serve'],
      interpreter: 'none',
      instances: 1,
      exec_mode: 'fork',
      autorestart: true,
      max_restarts: 10,
      min_uptime: '10s',
      max_memory_restart: '160M',
      env: {
        TIDOC_HUB_HOST: '127.0.0.1',
        TIDOC_HUB_PORT: '8767',
        TIDOC_HUB_PREFIX: '/tidoc-hub',
        TIDOC_HUB_DATA_DIR: '/opt/bitfsae-tidoc-hub/data',
        TIDOC_HUB_ENV_FILE: '/opt/bitfsae-tidoc-hub/.env',
      },
    },
  ],
};
