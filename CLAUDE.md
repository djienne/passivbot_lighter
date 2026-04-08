# Passivbot Lighter - Development Notes

## Docker Deployment

- **Always use `docker compose`** to manage the lighter bot container (not raw `docker run`)
- The `docker-compose.yml` mounts `./:/app/` as a volume, so code changes are picked up on restart without rebuilding
- Rebuild image: `docker compose build passivbot-lighter-live`
- Restart: `docker compose up -d passivbot-lighter-live`
- Logs: `docker logs passivbot-lighter-live --tail 50 -f`
- The `passivbot-hype-live` container runs a separate strategy -- never touch it
