<?php
/**
 * Internal WP-Bench verifier entrypoint for wp eval-file.
 *
 * Payload transport: JSON on stdin (preferred). A base64-encoded JSON
 * command argument is still accepted as a deprecated fallback; it is
 * limited by OS argv size and leaks payload content into process
 * listings, so new callers must use stdin.
 */

use WPBench\Runtime\Verifier;

require_once __DIR__ . '/src/class-sandbox.php';
require_once __DIR__ . '/src/class-static-analysis.php';
require_once __DIR__ . '/src/class-verifier.php';

/**
 * Read the verification payload from stdin or the legacy argument.
 *
 * @param array<int, mixed> $args Positional args from WP-CLI.
 * @return array<string, mixed> Decoded payload.
 */
function wp_bench_read_payload( array $args ): array {
	$stdin_json = file_get_contents( 'php://stdin' );
	if ( is_string( $stdin_json ) && '' !== trim( $stdin_json ) ) {
		$payload = json_decode( $stdin_json, true );
		if ( ! is_array( $payload ) ) {
			WP_CLI::error( 'Stdin payload is not valid JSON.' );
		}
		return $payload;
	}

	// Deprecated: base64 argument transport.
	$payload_b64 = $args[0] ?? '';
	if ( ! is_string( $payload_b64 ) || '' === $payload_b64 ) {
		WP_CLI::error( 'Missing payload: pass JSON via stdin (preferred) or a base64 argument (deprecated).' );
	}

	$payload_json = base64_decode( $payload_b64, true );
	if ( false === $payload_json ) {
		WP_CLI::error( 'Invalid payload encoding.' );
	}

	$payload = json_decode( $payload_json, true );
	if ( ! is_array( $payload ) ) {
		WP_CLI::error( 'Payload is not valid JSON.' );
	}

	return $payload;
}

$payload  = wp_bench_read_payload( $args );
$verifier = new Verifier();
WP_CLI::line( wp_json_encode( $verifier->verify_payload( $payload ), JSON_PRETTY_PRINT ) );
