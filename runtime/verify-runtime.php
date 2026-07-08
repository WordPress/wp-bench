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
require_once __DIR__ . '/src/class-verifier.php';

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
