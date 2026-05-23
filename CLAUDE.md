# Passivbot Lighter - Development Notes

## Python Environment

- **Always use the `passivbot` conda environment** for running scripts (backtest, optimize, download, etc.)
- Conda env path: `C:\Users\david\miniconda3\envs\passivbot`
- Run scripts with: `& "C:\Users\david\miniconda3\envs\passivbot\python.exe" src\backtest.py ...`
- Set `PYTHONPATH` to `src\` when running outside the `src` directory: `$env:PYTHONPATH = "C:\Users\david\Desktop\freqtrade\passivbot_lighter\src"`
- The `.venv310` virtualenv exists but is incomplete -- do not use it

## Docker Deployment

- **Always use `docker compose`** to manage the lighter bot container (not raw `docker run`)
- The `docker-compose.yml` mounts `./:/app/` as a volume, so code changes are picked up on restart without rebuilding
- Rebuild image: `docker compose build passivbot-lighter-live`
- Restart: `docker compose up -d passivbot-lighter-live`
- Logs: `docker logs passivbot-lighter-live --tail 50 -f`
- The `passivbot-hype-live` container runs a separate strategy -- never touch it
