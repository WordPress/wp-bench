<?php
/**
 * Required by the WP-CLI config Shell_Verifier gives candidate scripts: log the PHP files this process loaded.
 * WP-CLI and core files are skipped; candidate code can only enter through wp-content, the root, or /tmp.
 */
register_shutdown_function( static function (): void {
	$core = defined( 'ABSPATH' ) ? '|' . preg_quote( ABSPATH, '#' ) . 'wp-(includes|admin)/' : '';
	$files = preg_grep( '#^(phar://' . $core . ')#', get_included_files(), PREG_GREP_INVERT );
	file_put_contents( getenv( 'WP_CLI_CONFIG_PATH' ) . '.loaded', implode( "\n", $files ) . "\n", FILE_APPEND | LOCK_EX );
} );
