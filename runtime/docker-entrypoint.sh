#!/bin/bash
set -euo pipefail

cd /var/www/html

: "${WORDPRESS_DB_NAME:=wordpress}"
: "${WORDPRESS_DB_USER:=root}"
: "${WORDPRESS_DB_PASSWORD:=password}"
: "${WORDPRESS_DB_HOST:=mysql}"
: "${WORDPRESS_SITE_URL:=http://localhost}"
: "${WORDPRESS_SITE_TITLE:=WP Bench}" 
: "${WORDPRESS_ADMIN_USER:=admin}"
: "${WORDPRESS_ADMIN_PASSWORD:=password}"
: "${WORDPRESS_ADMIN_EMAIL:=admin@example.com}"

until mysql -h "$WORDPRESS_DB_HOST" -u "$WORDPRESS_DB_USER" -p"$WORDPRESS_DB_PASSWORD" -e 'SELECT 1' >/dev/null 2>&1; do
  >&2 echo "Waiting for database $WORDPRESS_DB_HOST..."
  sleep 2
done

if [ ! -f wp-config.php ]; then
  wp config create \
    --dbname="$WORDPRESS_DB_NAME" \
    --dbuser="$WORDPRESS_DB_USER" \
    --dbpass="$WORDPRESS_DB_PASSWORD" \
    --dbhost="$WORDPRESS_DB_HOST" \
    --skip-check \
    --allow-root
fi

# Resolve DB_NAME at runtime rather than baking the literal `wp config create`
# writes. The harness runs pooled tests by setting WORDPRESS_DB_NAME per
# command so each concurrent worker grades in its own database, and it reaches
# this container through `docker exec`, which never re-runs this entrypoint --
# so a literal here can never be revised and the override would be silently
# inert, leaving every worker on one shared database. `docker exec` does
# inherit the container's environment, so the unset case falls back to the
# name this container was started with and single-runtime behavior is
# unchanged. Runs on every start, not just creation, so a wp-config.php left
# in a volume by an older image is upgraded too.
wp config set DB_NAME "getenv('WORDPRESS_DB_NAME') ?: '$WORDPRESS_DB_NAME'" \
  --raw \
  --allow-root

if ! wp core is-installed --allow-root >/dev/null 2>&1; then
  wp core install \
    --url="$WORDPRESS_SITE_URL" \
    --title="$WORDPRESS_SITE_TITLE" \
    --admin_user="$WORDPRESS_ADMIN_USER" \
    --admin_password="$WORDPRESS_ADMIN_PASSWORD" \
    --admin_email="$WORDPRESS_ADMIN_EMAIL" \
    --skip-email \
    --allow-root
fi

wp plugin activate wp-bench-runtime --allow-root

exec "$@"
