FROM wordpress:cli-php8.2

ENV WORDPRESS_VERSION=7.1 \
    WP_CLI_ALLOW_ROOT=1

USER root
RUN apk add --no-cache mysql-client

WORKDIR /var/www/html

RUN rm -rf ./* && \
    php -d memory_limit=512M "$(command -v wp)" core download --version=${WORDPRESS_VERSION} --force

COPY wp-bench-runtime.php /var/www/html/wp-content/plugins/wp-bench-runtime/wp-bench-runtime.php
COPY verify-runtime.php /var/www/html/wp-content/plugins/wp-bench-runtime/verify-runtime.php
COPY src /var/www/html/wp-content/plugins/wp-bench-runtime/src
COPY docker-entrypoint.sh /usr/local/bin/wp-bench-entrypoint
RUN chmod +x /usr/local/bin/wp-bench-entrypoint

ENTRYPOINT ["wp-bench-entrypoint"]
CMD ["wp", "--info"]
