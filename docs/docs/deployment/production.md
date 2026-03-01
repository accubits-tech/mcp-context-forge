# 🚀 Production Deployment

Deploy MCP Foundry to a production server using pre-built container images. The production server needs **no source code** — just two files: `docker-compose.prod.yml` and `.env`.

---

## Prerequisites

| Requirement | Minimum | Notes |
|---|---|---|
| Docker Engine | 24+ | Or Podman 4.5+ with compose support |
| Docker Compose | v2+ | Bundled with Docker Desktop |
| RAM | 2 GB | 4 GB recommended |
| Disk | 10 GB | For images + database volumes |

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                   Production Server                  │
│                                                      │
│  ┌────────┐   ┌─────────┐   ┌──────────┐           │
│  │ Nginx  │──▶│ Gateway │──▶│ Postgres │           │
│  │ :8080  │   │ internal│   │ internal │           │
│  └────────┘   └────┬────┘   └──────────┘           │
│                    │                                 │
│  ┌──────────┐     │        ┌──────────┐            │
│  │ Frontend │     └───────▶│  Redis   │            │
│  │  :3000   │              │ internal │            │
│  └──────────┘              └──────────┘            │
│                                                      │
│  All images pulled from ghcr.io/accubits-tech/       │
│  Only Nginx (:8080) and Frontend (:3000) are exposed │
└──────────────────────────────────────────────────────┘
```

**Services:**

| Service | Image | Purpose |
|---|---|---|
| `gateway` | `ghcr.io/accubits-tech/mcp-foundry:latest` | Backend API server |
| `frontend` | `ghcr.io/accubits-tech/mcp-foundry-fe:latest` | React admin dashboard |
| `nginx` | `ghcr.io/accubits-tech/mcp-foundry-nginx:latest` | Caching reverse proxy |
| `postgres` | `postgres:17` | Primary database |
| `redis` | `redis:latest` | Cache (password-protected) |

---

## Step 1: Build and Push Images

On a **developer machine** with the source code:

### Authenticate with GitHub Container Registry

```bash
echo "YOUR_GITHUB_PAT" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

The PAT needs the `write:packages` scope. Generate one at **GitHub > Settings > Developer settings > Personal access tokens**.

### Build all images

```bash
# Backend
cd mcp-foundry-be
docker build -f Containerfile.lite -t ghcr.io/accubits-tech/mcp-foundry:latest .

# Frontend
cd ../mcp-foundry-fe
docker build -t ghcr.io/accubits-tech/mcp-foundry-fe:latest .

# Nginx
cd ../mcp-foundry-be
docker build -t ghcr.io/accubits-tech/mcp-foundry-nginx:latest ./nginx
```

To set a custom API base URL for the frontend at build time:

```bash
docker build --build-arg VITE_API_BASE_URL=https://api.yourdomain.com \
  -t ghcr.io/accubits-tech/mcp-foundry-fe:latest ../mcp-foundry-fe
```

### Push to registry

```bash
docker push ghcr.io/accubits-tech/mcp-foundry:latest
docker push ghcr.io/accubits-tech/mcp-foundry-fe:latest
docker push ghcr.io/accubits-tech/mcp-foundry-nginx:latest
```

---

## Step 2: Prepare the Production Server

### Copy files to the server

Only two files are needed:

```bash
scp mcp-foundry-be/docker-compose.prod.yml user@server:~/mcp-foundry/
scp mcp-foundry-be/.env user@server:~/mcp-foundry/
```

### Create the `.env` file

Copy the example and fill in your values:

```bash
cp .env.example.prod .env
```

**Required changes** (all marked `CHANGE_ME_` in the example):

| Variable | Description | Example |
|---|---|---|
| `POSTGRES_PASSWORD` | PostgreSQL password | `s3cure-db-p@ss!` |
| `REDIS_PASSWORD` | Redis authentication password | `r3d1s-s3cret!` |
| `JWT_SECRET_KEY` | JWT signing secret (min 32 chars) | `a-long-random-string-here-32chars` |
| `AUTH_ENCRYPTION_SECRET` | AES key for secure auth storage | `another-random-secret-here` |
| `PLATFORM_ADMIN_PASSWORD` | Initial admin account password | `Adm1n-p@ssw0rd!` |

**Recommended changes:**

| Variable | Default | Recommendation |
|---|---|---|
| `APP_DOMAIN` | `localhost` | Your production domain (e.g. `myapp.com`) |
| `PLATFORM_ADMIN_EMAIL` | `admin@example.com` | Your actual admin email |
| `ALLOWED_ORIGINS` | `*` | Your frontend domain(s) (e.g. `https://myapp.com`) |

**Optional tuning:**

| Variable | Default | Notes |
|---|---|---|
| `NGINX_PORT` | `8080` | Public-facing proxy port |
| `FRONTEND_PORT` | `3000` | Frontend UI port |
| `LOG_LEVEL` | `ERROR` | Set to `INFO` or `DEBUG` for troubleshooting |
| `LOG_FORMAT` | `json` | Use `text` for human-readable logs |
| `SECURE_COOKIES` | `true` | Set `false` only if not using HTTPS |
| `MCPGATEWAY_UI_ENABLED` | `true` | Built-in admin UI |
| `PLUGINS_ENABLED` | `true` | Plugin framework |
| `MCPGATEWAY_CATALOG_ENABLED` | `true` | MCP server catalog |
| `MCPGATEWAY_A2A_ENABLED` | `true` | Agent-to-Agent features |

**SSO (optional):**

Uncomment the SSO variables in `.env` to enable Keycloak or other providers. See the full list in `.env.example.prod`.

Full example `.env`:

```bash
# Ports
NGINX_PORT=8080
FRONTEND_PORT=3000

# Domain
APP_DOMAIN=yourcompany.com

# PostgreSQL
POSTGRES_USER=postgres
POSTGRES_PASSWORD=s3cure-db-p@ss!
POSTGRES_DB=mcp

# Redis
REDIS_PASSWORD=r3d1s-s3cret!

# JWT
JWT_ALGORITHM=HS256
JWT_SECRET_KEY=a-long-random-string-here-32chars
AUTH_ENCRYPTION_SECRET=another-random-secret-here

# Admin
PLATFORM_ADMIN_EMAIL=admin@yourcompany.com
PLATFORM_ADMIN_PASSWORD=Adm1n-p@ssw0rd!

# CORS
ALLOWED_ORIGINS=https://yourcompany.com

# Features
REQUIRE_TOKEN_EXPIRATION=true
MCPGATEWAY_UI_ENABLED=true
MCPGATEWAY_ADMIN_API_ENABLED=true
PLUGINS_ENABLED=true
SECURE_COOKIES=true
LOG_LEVEL=ERROR
LOG_FORMAT=json
```

---

## Step 3: Deploy

On the **production server**:

```bash
cd ~/mcp-foundry

# Login to pull images (if private)
echo "YOUR_GITHUB_PAT" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin

# Start the stack
docker compose -f docker-compose.prod.yml up -d
```

### Verify all services are healthy

```bash
docker compose -f docker-compose.prod.yml ps
```

Expected output — all services should show `healthy`:

```
NAME        IMAGE                                          STATUS
frontend    ghcr.io/accubits-tech/mcp-foundry-fe:latest    Up (healthy)
gateway     ghcr.io/accubits-tech/mcp-foundry:latest       Up (healthy)
nginx       ghcr.io/accubits-tech/mcp-foundry-nginx:latest Up (healthy)
postgres    postgres:17                                     Up (healthy)
redis       redis:latest                                    Up (healthy)
```

### Test endpoints

```bash
# Gateway health (via Nginx proxy — gateway is not exposed directly)
curl http://localhost:8080/health
# Expected: {"status":"healthy"}

# Frontend
curl -s http://localhost:3000 | head -5
# Expected: HTML response
```

---

## Updating

To deploy a new version:

### On the developer machine

```bash
# Rebuild and push updated images
docker build -f Containerfile.lite -t ghcr.io/accubits-tech/mcp-foundry:latest .
docker push ghcr.io/accubits-tech/mcp-foundry:latest

# Repeat for frontend/nginx if changed
```

### On the production server

```bash
cd ~/mcp-foundry
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

Only containers with updated images will be recreated. Database volumes are preserved.

---

## Operations

### View logs

```bash
# All services
docker compose -f docker-compose.prod.yml logs -f

# Single service
docker compose -f docker-compose.prod.yml logs -f gateway
```

### Restart a service

```bash
docker compose -f docker-compose.prod.yml restart gateway
```

### Stop the stack

```bash
docker compose -f docker-compose.prod.yml down
```

### Stop and remove all data (destructive)

```bash
docker compose -f docker-compose.prod.yml down -v
```

!!! warning
    The `-v` flag deletes all volumes including the database. Use only when you want a clean slate.

### Shell into a container

```bash
docker compose -f docker-compose.prod.yml exec gateway /bin/sh
docker compose -f docker-compose.prod.yml exec postgres psql -U postgres -d mcp
docker compose -f docker-compose.prod.yml exec redis redis-cli -a "$REDIS_PASSWORD"
```

---

## Image Visibility

By default, GHCR packages are **private**. To allow pulling without authentication:

1. Go to **GitHub > Organization (accubits-tech) > Packages**
2. Select each package (mcp-foundry, mcp-foundry-fe, mcp-foundry-nginx)
3. **Package settings > Change visibility > Public**

If kept private, every machine pulling images must `docker login ghcr.io` first.

---

## Troubleshooting

### Gateway fails to start

Check if PostgreSQL is healthy first:

```bash
docker compose -f docker-compose.prod.yml logs postgres
docker compose -f docker-compose.prod.yml exec postgres pg_isready -U postgres
```

### Redis connection refused

Verify the Redis password matches between `.env` and the running container:

```bash
docker compose -f docker-compose.prod.yml exec redis redis-cli -a "$REDIS_PASSWORD" ping
# Expected: PONG
```

### Frontend shows blank page

The `VITE_API_BASE_URL` is baked in at build time. If the frontend can't reach the backend, rebuild the frontend image with the correct URL:

```bash
docker build --build-arg VITE_API_BASE_URL=https://your-api-url.com \
  -t ghcr.io/accubits-tech/mcp-foundry-fe:latest .
```

### Check resource usage

```bash
docker stats --no-stream
```
