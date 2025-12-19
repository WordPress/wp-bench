<?php
/**
 * WP-CLI bridge for verifying payloads from the Python harness.
 */

declare(strict_types=1);

namespace WPBench\Runtime;

use WP_CLI;
use WP_CLI\Utils;

class CLI_Verifier {
	private Static_Analysis $static_analysis;
	private Sandbox $sandbox;

	public function __construct() {
		$this->static_analysis = new Static_Analysis();
		$this->sandbox        = new Sandbox();
	}

	/**
	 * Execute verification from a Base64 payload.
	 *
	 * ## OPTIONS
	 *
	 * [--payload=<payload>]
	 * : Base64 encoded JSON payload containing code and verification spec.
	 *
	 * [--format=<format>]
	 * : Output format (json or table).
	 */
	public function __invoke( array $args, array $assoc_args ): void {
		$payload_b64 = $assoc_args['payload'] ?? null;
		if ( ! $payload_b64 ) {
			WP_CLI::error( 'Missing --payload argument.' );
		}

		$payload_json = base64_decode( (string) $payload_b64, true );
		if ( false === $payload_json ) {
			WP_CLI::error( 'Invalid payload encoding.' );
		}

		$payload = json_decode( $payload_json, true );
		if ( ! is_array( $payload ) ) {
			WP_CLI::error( 'Payload is not valid JSON.' );
		}

		$code_value = $payload['code'] ?? '';
		$code       = is_string( $code_value ) ? $code_value : '';

		$static_checks  = $payload['static_checks'] ?? [];
		$runtime_checks = $payload['runtime_checks'] ?? [];

		$static_result  = $this->static_analysis->check( $code, is_array( $static_checks ) ? $static_checks : [] );
		$runtime_result = $this->sandbox->execute_and_verify( $code, is_array( $runtime_checks ) ? $runtime_checks : [] );

		$success = $runtime_result['score'] >= 0.999 && $static_result['score'] >= 0.999;

		$response = [
			'success'    => $success,
			'static'     => $static_result,
			'runtime'    => $runtime_result,
			'assertions' => $runtime_result['details']['assertions'] ?? [],
			'version'    => '1.0.0',
		];

		$format = $assoc_args['format'] ?? 'json';
        if ( 'json' === $format ) {
            WP_CLI::line( \wp_json_encode( $response, JSON_PRETTY_PRINT ) );
			return;
		}

		WP_CLI::success( sprintf( 'Static: %.2f, Runtime: %.2f', $static_result['score'], $runtime_result['score'] ) );
	}
}
