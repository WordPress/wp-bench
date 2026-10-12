<?php
/**
 * Internal WP-Bench verifier entrypoint for wp eval-file.
 *
 * Payload transport: JSON on stdin. Payloads must not travel as command
 * arguments — argv has OS size limits and leaks payload content into
 * process listings.
 */

use WPBench\Runtime\Verifier;

require_once __DIR__ . '/src/class-sandbox.php';
require_once __DIR__ . '/src/class-static-analysis.php';
require_once __DIR__ . '/src/class-artifact-installer.php';
require_once __DIR__ . '/src/class-verifier.php';
require_once __DIR__ . '/src/class-shell-verifier.php';

// This is an offline evaluation site. Core maintenance requests are unrelated
// to candidates and otherwise make admin hooks depend on WordPress.org access.
foreach ( array(
	'admin_init' => array( '_maybe_update_core', '_maybe_update_plugins', '_maybe_update_themes' ),
	'load-plugins.php' => array( 'wp_update_plugins' ),
	'load-themes.php' => array( 'wp_update_themes' ),
	'load-update.php' => array( 'wp_update_plugins', 'wp_update_themes' ),
	'load-update-core.php' => array( 'wp_update_plugins', 'wp_update_themes' ),
	'wp_version_check' => array( 'wp_version_check' ),
	'wp_update_plugins' => array( 'wp_update_plugins' ),
	'wp_update_themes' => array( 'wp_update_themes' ),
	'wp_maybe_auto_update' => array( 'wp_maybe_auto_update' ),
) as $hook => $callbacks ) {
	foreach ( $callbacks as $callback ) {
		remove_action( $hook, $callback );
	}
}

$payload_json = file_get_contents( 'php://stdin' );
if ( ! is_string( $payload_json ) || '' === trim( $payload_json ) ) {
	WP_CLI::error( 'Missing payload: pass JSON via stdin.' );
}

$payload = json_decode( $payload_json, true );
if ( ! is_array( $payload ) ) {
	WP_CLI::error( 'Stdin payload is not valid JSON.' );
}

$verifier = new Verifier();
WP_CLI::line( wp_json_encode( $verifier->verify_payload( $payload ), JSON_PRETTY_PRINT ) );
