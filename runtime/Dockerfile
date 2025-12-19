FROM wordpress:cli-php8.2

ENV WORDPRESS_VERSION=6.9 \
    WP_CLI_ALLOW_ROOT=1

RUN apt-get update && apt-get install -y default-mysql-client && rm -rf /var/lib/apt/lists/*

WORKDIR /var/www/html

RUN rm -rf ./* && \
    wp core download --version=${WORDPRESS_VERSION} --force

COPY wp-bench-runtime.php /var/www/html/wp-content/plugins/wp-bench-runtime/wp-bench-runtime.php
COPY src /var/www/html/wp-content/plugins/wp-bench-runtime/src
COPY docker-entrypoint.sh /usr/local/bin/wp-bench-entrypoint
RUN chmod +x /usr/local/bin/wp-bench-entrypoint

ENTRYPOINT ["wp-bench-entrypoint"]
CMD ["wp", "--info"]
