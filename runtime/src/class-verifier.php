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
	 * Verify a candidate artifact against static and runtime checks.
	 *
	 * Supported artifact kinds:
	 * - php_snippet (default): `code` string executed via the sandbox.
	 * - wp_plugin_files: `files` map installed as a plugin before the
	 *   assertions run; static analysis covers the concatenated files.
	 *
	 * @param array<string, mixed> $payload Verification payload.
	 * @return array<string, mixed>
	 */
	public function verify_payload( array $payload ): array {
		$kind_value = $payload['artifact_kind'] ?? 'php_snippet';
		$kind       = is_string( $kind_value ) ? $kind_value : 'php_snippet';

		$static_checks_value  = $payload['static_checks'] ?? [];
		$runtime_checks_value = $payload['runtime_checks'] ?? [];
		$static_checks        = is_array( $static_checks_value ) ? $static_checks_value : [];
		$runtime_checks       = is_array( $runtime_checks_value ) ? $runtime_checks_value : [];

		if ( 'wp_plugin_files' === $kind ) {
			return $this->verify_plugin_files( $payload, $static_checks, $runtime_checks );
		}

		$code_value = $payload['code'] ?? '';
		$code       = is_string( $code_value ) ? $code_value : '';

		$static_result  = $this->static_analysis->check( $code, $static_checks );
		$runtime_result = $this->sandbox->execute_and_verify( $code, $runtime_checks );

		return $this->build_result( $static_result, $runtime_result, $kind );
	}

	/**
	 * Verify a plugin-files artifact: install, assert, clean up.
	 *
	 * @param array<string, mixed> $payload        Full payload.
	 * @param array<string, mixed> $static_checks  Static check config.
	 * @param array<string, mixed> $runtime_checks Runtime check config.
	 * @return array<string, mixed>
	 */
	private function verify_plugin_files( array $payload, array $static_checks, array $runtime_checks ): array {
		$files_value = $payload['files'] ?? [];
		$files       = is_array( $files_value ) ? $files_value : [];

		// Static analysis runs over all files so patterns can match any of them.
		$combined = implode( "\n\n", array_map( 'strval', array_values( $files ) ) );

		$installer      = new Artifact_Installer();
		$install_result = $installer->install( $files );

		if ( true !== $install_result['success'] ) {
			$installer->cleanup();
			$error = isset( $install_result['error'] ) && is_string( $install_result['error'] )
				? $install_result['error']
				: 'Plugin artifact installation failed.';
			return [
				'success' => false,
				'static'  => $this->static_analysis->check( $combined, $static_checks ),
				'runtime' => [
					'score'   => 0.0,
					'details' => [
						'assertions'    => [
							[
								'type'        => 'artifact_install_error',
								'description' => $error,
								'passed'      => false,
							],
						],
						'total_weight'  => 0,
						'passed_weight' => 0,
					],
				],
				'version' => '1.0.0',
			];
		}

		try {
			$static_result = $this->static_analysis->check( $combined, $static_checks );
			// The plugin's main file is already loaded; run assertions
			// without additional candidate code.
			$runtime_result = $this->sandbox->execute_and_verify( '', $runtime_checks );
		} finally {
			$installer->cleanup();
		}

		return $this->build_result( $static_result, $runtime_result, 'wp_plugin_files' );
	}

	/**
	 * Assemble the standard verifier response.
	 *
	 * @param array{score: float, details: array<string, mixed>} $static_result  Static outcome.
	 * @param array{score: float, details: array<string, mixed>} $runtime_result Runtime outcome.
	 * @param string                                             $kind           Artifact kind.
	 * @return array<string, mixed>
	 */
	private function build_result( array $static_result, array $runtime_result, string $kind ): array {
		return [
			'success'       => $runtime_result['score'] >= 0.999 && $static_result['score'] >= 0.999,
			'artifact_kind' => $kind,
			'static'        => $static_result,
			'runtime'       => $runtime_result,
			'assertions'    => $runtime_result['details']['assertions'] ?? [],
			'version'       => '1.0.0',
		];
	}
}
