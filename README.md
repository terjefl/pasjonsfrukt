# 🍹 pasjonsfrukt

Scrape PodMe podcast streams to mp3 and host with RSS feed.

> A valid PodMe subscription is required to access premium content.

This is a personal fork of [mathiazom/pasjonsfrukt](https://github.com/mathiazom/pasjonsfrukt) with the following additions:
- **Podcast index page** (`GET /`) — a mobile-friendly HTML overview of all configured podcasts, with RSS links and one-tap buttons for Overcast and Pocket Casts.

---

### Docker Compose

The recommended way to run pasjonsfrukt is with Docker Compose.

**`compose.yml`**
```yaml
services:
  pasjonsfrukt:
    image: ghcr.io/terjefl/pasjonsfrukt:latest
    container_name: pasjonsfrukt
    restart: unless-stopped
    ports:
      - "8100:8000"
    environment:
      - TZ=Europe/Oslo
    volumes:
      - /path/to/config.yaml:/app/config.yaml:ro
      - /path/to/crontab:/etc/cron.d/pasjonsfrukt-crontab:ro
      - /path/to/podcast/files:/app/yield
    healthcheck:
      test: ["CMD-SHELL", "curl -f http://localhost:8000/openapi.json || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 15s
```

The crontab controls when harvesting runs. Example:

```cron
0 4 * * * root pasjonsfrukt harvest >> /var/log/pasjonsfrukt.log 2>&1
```

---

### Configuration

Copy [`config.template.yaml`](config.template.yaml) to `config.yaml` and fill in your details.

```yaml
host: "https://your-domain-here"
yield_dir: "yield"
#secret: "optional-access-secret"

#api:
#  language: NO        # NO, SE, FI (default: NO)
#  region: NO          # NO, SE, FI (default: NO)
#  request_timeout: 30.0
#  disable_credentials_storage: false

auth:
  email: "your@email.com"
  password: "yourpassword"

podcasts:
  podcast-slug:
    feed_name: "feed"
    most_recent_episodes_limit: 50
  another-podcast-slug:
```

**`host`** is used to build links in the RSS feeds and must be publicly reachable by your podcast app.

**`secret`** adds a `?secret=<value>` query parameter requirement on all endpoints — useful for keeping feeds semi-private when hosted publicly.

**`api`** lets you configure PodMe region and language (for SE/FI content), request timeout, and credential storage behaviour.

---

### Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | HTML index of all configured podcasts |
| `GET /{slug}` | RSS feed for a podcast |
| `GET /{slug}/{episode_id}` | Episode audio file |

If a `secret` is configured, append `?secret=<value>` to all requests.

#### Podcast index page

The index page (`GET /`) lists all configured podcasts as cards with title, description, and direct links to subscribe in Overcast or Pocket Casts. Useful as a private landing page for your feeds.

---

### Checking CDN compatibility before adding a podcast

Some PodMe podcasts distribute episodes via Acast's CDN (`flex2.acast.com`) instead of PodMe's own CDN. Acast episodes will always fail with 403 Forbidden and cannot be harvested — and the split between PodMe and Acast episodes within a single series is unpredictable, meaning some episodes may work while others silently fail.

Before adding a new slug to `config.yaml`, run:

```sh
pasjonsfrukt check <slug>
```

This fetches the 20 most recent episodes and checks each download URL:

```
  ✅ 12345: PODME  (https://dd-podme.akamaized.net/...)
  ❌ 12346: ACAST  (https://flex2.acast.com/s/...)

Resultat for 'min-serie': 1/2 på PodMe-CDN
❌ Ikke trygg – 1 episode er på Acast-CDN og vil feile med 403
```

Only add slugs where 100% of episodes are on PodMe CDN. Use `-n` to check more episodes:

```sh
pasjonsfrukt check <slug> -n 50
```

---

### Development

#### Formatting

```sh
poe fmt
```

> Uses [Black](https://black.readthedocs.io/en/stable/)
