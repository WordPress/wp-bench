FROM wordpress:cli-php8.2

ENV WORDPRESS_VERSION=7.1 \
    WP_CLI_ALLOW_ROOT=1 \
    WP_BENCH_SQLITE_PATH=/var/www/html/wp-content/database/.ht.sqlite

ARG SQLITE_INTEGRATION_VERSION=3.0.2
ARG SQLITE_INTEGRATION_SHA256=a7c506b95283e90544aa9dcf0096cb5a5eea4afd7d690ae350940a50654c8bfd

USER root
RUN php -r 'exit(extension_loaded("pdo_sqlite") ? 0 : 1);'

WORKDIR /var/www/html

RUN rm -rf ./* && \
    php -d memory_limit=512M "$(command -v wp)" core download --version=${WORDPRESS_VERSION} --force

# Install the database drop-in before WordPress ever boots. No DB server or
# plugin activation is needed to load the official MySQL-to-SQLite adapter.
RUN curl -fsSL "https://github.com/WordPress/sqlite-database-integration/releases/download/v${SQLITE_INTEGRATION_VERSION}/plugin-sqlite-database-integration.zip" -o /tmp/sqlite.zip && \
    echo "${SQLITE_INTEGRATION_SHA256}  /tmp/sqlite.zip" | sha256sum -c - && \
    unzip -q /tmp/sqlite.zip -d /tmp/sqlite && \
    mv /tmp/sqlite/plugin-sqlite-database-integration wp-content/plugins/sqlite-database-integration && \
    cp wp-content/plugins/sqlite-database-integration/db.copy wp-content/db.php && \
    rm -rf /tmp/sqlite /tmp/sqlite.zip

COPY wp-bench-runtime.php /var/www/html/wp-content/plugins/wp-bench-runtime/wp-bench-runtime.php
COPY verify-runtime.php /var/www/html/wp-content/plugins/wp-bench-runtime/verify-runtime.php
COPY src /var/www/html/wp-content/plugins/wp-bench-runtime/src
COPY sqlite-snapshot.php /var/www/html/wp-content/plugins/wp-bench-runtime/sqlite-snapshot.php
COPY docker-entrypoint.sh /usr/local/bin/wp-bench-entrypoint
RUN chmod +x /usr/local/bin/wp-bench-entrypoint
RUN chown -R www-data:www-data /var/www/html
# Keep pristine files outside WordPress's writable mount. Disposable graders
# copy these into a bounded private tmpfs; Compose uses its private volume.
RUN mkdir -p /opt/wp-bench-wordpress && \
    cp -a /var/www/html/. /opt/wp-bench-wordpress/ && \
    rm -rf /var/www/html/*

HEALTHCHECK --interval=1s --timeout=2s --start-period=120s \
    CMD test -f /tmp/wp-bench-ready || exit 1
ENTRYPOINT ["wp-bench-entrypoint"]
CMD ["sleep", "infinity"]
