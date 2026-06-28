# 🍹 pasjonsfrukt

Scrape PodMe podcast streams to mp3 and host with RSS feed.

> A valid PodMe subscription is required to access premium content.

This is a personal fork of [mathiazom/pasjonsfrukt](https://github.com/mathiazom/pasjonsfrukt) with the following additions:
- **Podcast index page** (`GET /`) — a mobile-friendly HTML overview of all configured podcasts, with RSS links and one-tap buttons for Overcast and Pocket Casts.
- **Multi-user support** — define multiple users in config, each with their own secret. Each user gets private RSS feeds with their secret embedded in episode URLs.
- **Disable index** — optional config flag to turn off the index page entirely.

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

auth:
  email: "your@email.com"
  password: "yourpassword"

podcasts:
  podcast-slug:
    feed_name: "feed"
    most_recent_episodes_limit: 50
  another-podcast-slug:
```

#### `host`

The public base URL of your instance. Used to build links in RSS feeds and on the index page — must be reachable by your podcast app.

#### `yield_dir`

Directory where downloaded MP3 files and generated RSS feeds are stored. Defaults to `"yield"`.

#### `secret`

Adds a `?secret=<value>` query parameter requirement on all endpoints. Useful for keeping feeds semi-private when hosted publicly. All clients share the same secret.

```yaml
secret: "my-shared-secret"
```

#### `users`

Per-user secrets for multi-user setups. Each user gets their own private RSS feeds — episode URLs in each feed contain that user's secret, so podcast apps can fetch audio without extra configuration. MP3 files are not duplicated; the secret is only a URL parameter.

```yaml
users:
  - alias: "alice"
    secret: "alice-secret"
  - alias: "bob"
    secret: "bob-secret"
```

When `users` is configured it takes precedence over `secret`. After adding or changing users, run `pasjonsfrukt sync` to regenerate all feed files.

> **Migrating from `secret` to `users`:** The existing `<feed_name>.xml` files on disk become orphaned — the server now looks for `<feed_name>-<alias>.xml` instead. Run `pasjonsfrukt sync` to generate the new per-user feed files, then remove the old ones manually.

#### `disable_index`

Set to `true` to disable the HTML index page. `GET /` will return 404.

```yaml
disable_index: true
```

Defaults to `false`.

#### `api`

PodMe API settings. All fields are optional.

```yaml
api:
  language: NO                     # NO, SE, FI (default: NO)
  region: NO                       # NO, SE, FI (default: NO)
  request_timeout: 30.0            # HTTP timeout in seconds (default: 30.0)
  disable_credentials_storage: false  # Don't cache auth tokens to disk (default: false)
  max_concurrent_downloads: 3      # Max simultaneous episode downloads (default: 3)
```

`max_concurrent_downloads` limits how many episodes are downloaded in parallel during a harvest. Reduce it if you hit rate limits or want to lower network load.

#### `podcasts`

A map of podcast slugs to per-podcast settings. The slug is the identifier used in the PodMe URL.

```yaml
podcasts:
  podcast-slug:
    feed_name: "feed"              # Base name for the RSS XML file (default: "feed")
    most_recent_episodes_limit: 50 # Only harvest the N most recent episodes (default: no limit)
  another-podcast-slug:            # Minimal entry — all settings use defaults
```

`feed_name` is the base filename of the RSS XML file stored on disk. **It has no effect on the URL** — the endpoint is always `GET /{slug}` regardless of this setting. The default (`"feed"`) is fine in most cases; only change it if you have a specific reason to name the file differently.

File location on disk:
- Single-secret / no-auth: `yield/{slug}/{feed_name}.xml`
- Multi-user: `yield/{slug}/{feed_name}-{alias}.xml` per user

---

### Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | HTML index of all configured podcasts |
| `GET /{slug}` | RSS feed for a podcast |
| `GET /{slug}/{episode_id}` | Episode audio file |

**With `secret`:** append `?secret=<value>` to all requests.

**With `users`:** append `?secret=<user-secret>` to all requests. The RSS feed at `GET /{slug}` will contain episode URLs pre-populated with that user's secret, so podcast apps fetch audio correctly without further configuration.

#### Podcast index page

The index page (`GET /`) lists all configured podcasts as cards with title, description, and direct links to subscribe in Overcast or Pocket Casts. Feed URLs on the page include the requesting user's secret automatically. Can be disabled with `disable_index: true`.

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
