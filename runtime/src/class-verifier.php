<?php
/**
 * Verification service for WP-Bench payloads.
 */

declare(strict_types=1);

namespace WPBench\Runtime;

class Verifier {
	private Static_Analysis $static_analysis;
	private Sandbox $sandbox;

	public function __construct() {
		$this->static_analysis = new Static_Analysis();
		$this->sandbox         = new Sandbox();
	}

	/**
	 * Verify candidate code against static and runtime checks.
	 *
	 * @param array<string, mixed> $payload Verification payload.
	 * @return array<string, mixed>
	 */
	public function verify_payload( array $payload ): array {
		$code_value = $payload['code'] ?? '';
		$code       = is_string( $code_value ) ? $code_value : '';

		$static_checks  = $payload['static_checks'] ?? [];
		$runtime_checks = $payload['runtime_checks'] ?? [];

		$static_result  = $this->static_analysis->check( $code, is_array( $static_checks ) ? $static_checks : [] );
		$runtime_result = $this->sandbox->execute_and_verify( $code, is_array( $runtime_checks ) ? $runtime_checks : [] );

		return [
			'success'    => $runtime_result['score'] >= 0.999 && $static_result['score'] >= 0.999,
			'static'     => $static_result,
			'runtime'    => $runtime_result,
			'assertions' => $runtime_result['details']['assertions'] ?? [],
			'version'    => '1.0.0',
		];
	}
}
