#!/bin/bash
set -euo pipefail

cd /var/www/html
rm -f /tmp/wp-bench-ready
if [ ! -f wp-load.php ]; then
  cp -R /opt/wp-bench-wordpress/. /var/www/html/
fi

: "${WP_BENCH_SQLITE_PATH:=/var/www/html/wp-content/database/.ht.sqlite}"
: "${WORDPRESS_SITE_URL:=http://localhost}"
: "${WORDPRESS_SITE_TITLE:=WP Bench}"
: "${WORDPRESS_ADMIN_USER:=admin}"
: "${WORDPRESS_ADMIN_PASSWORD:=password}"
: "${WORDPRESS_ADMIN_EMAIL:=admin@example.com}"

mkdir -p "$(dirname "$WP_BENCH_SQLITE_PATH")"

if [ ! -f wp-config.php ]; then
  wp config create \
    --dbname=wordpress \
    --dbuser=sqlite \
    --dbpass='' \
    --skip-check \
    --allow-root
fi

wp config set DB_ENGINE sqlite --allow-root
wp config set DB_DIR "$(dirname "$WP_BENCH_SQLITE_PATH")/" --allow-root
wp config set DB_FILE "$(basename "$WP_BENCH_SQLITE_PATH")" --allow-root
wp config set WP_DEBUG true --raw --allow-root
wp config set WP_ENVIRONMENT_TYPE local --allow-root

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
touch /tmp/wp-bench-ready

exec "$@"
