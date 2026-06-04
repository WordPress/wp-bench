<?php
/**
 * Internal WP-Bench verifier entrypoint for wp eval-file.
 */

declare(strict_types=1);

use WPBench\Runtime\Verifier;

require_once __DIR__ . '/src/class-sandbox.php';
require_once __DIR__ . '/src/class-static-analysis.php';
require_once __DIR__ . '/src/class-verifier.php';

$payload_b64 = $args[0] ?? '';
if ( ! is_string( $payload_b64 ) || '' === $payload_b64 ) {
	WP_CLI::error( 'Missing payload argument.' );
}

$payload_json = base64_decode( $payload_b64, true );
if ( false === $payload_json ) {
	WP_CLI::error( 'Invalid payload encoding.' );
}

$payload = json_decode( $payload_json, true );
if ( ! is_array( $payload ) ) {
	WP_CLI::error( 'Payload is not valid JSON.' );
}

$verifier = new Verifier();
WP_CLI::line( wp_json_encode( $verifier->verify_payload( $payload ), JSON_PRETTY_PRINT ) );
