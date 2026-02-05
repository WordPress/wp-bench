<?php
/**
 * WP-CLI bridge for executing Abilities API calls.
 */

declare(strict_types=1);

namespace WPBench\Runtime;

use WP_CLI;
use WP_Error;

class CLI_Ability {
	private Sandbox $sandbox;

	public function __construct() {
		$this->sandbox = new Sandbox();
	}

	/**
	 * Execute an ability from a Base64 payload.
	 *
	 * ## OPTIONS
	 *
	 * [--payload=<payload>]
	 * : Base64 encoded JSON payload containing ability name, input, and optional setup/teardown.
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

		$name   = $payload['ability'] ?? '';
		$input  = $payload['input'] ?? null;
		$setup  = $payload['setup'] ?? '';
		$teardown = $payload['teardown'] ?? '';
		$method = $payload['method'] ?? '';

		if ( ! is_string( $name ) || '' === $name ) {
			WP_CLI::error( 'Ability name is required.' );
		}

		if ( is_string( $setup ) && $setup !== '' ) {
			$setup_result = $this->sandbox->execute_snippet( $setup );
			if ( ! $setup_result['success'] ) {
				$this->render_response(
					array(
						'success' => false,
						'error'   => $setup_result['error'],
						'trace'   => $setup_result['trace'],
					),
					$assoc_args
				);
				return;
			}
		}

		if ( ! function_exists( 'wp_get_ability' ) ) {
			$this->render_response(
				array(
					'success' => false,
					'error'   => 'abilities_api_missing',
				),
				$assoc_args
			);
			return;
		}

		$ability = wp_get_ability( $name );
		if ( ! $ability ) {
			$this->render_response(
				array(
					'success' => false,
					'ability' => $name,
					'ability_found' => false,
					'error'   => 'ability_not_found',
				),
				$assoc_args
			);
			return;
		}

		$annotations = $ability->get_meta_item( 'annotations' );
		$is_readonly = ! empty( $annotations['readonly'] );
		$method_valid = true;
		$expected_method = null;

		if ( is_string( $method ) && '' !== $method ) {
			if ( $is_readonly && 'GET' !== $method ) {
				$method_valid = false;
				$expected_method = 'GET';
			}
			if ( ! $is_readonly && 'POST' !== $method ) {
				$method_valid = false;
				$expected_method = 'POST';
			}
			if ( ! $method_valid ) {
				$this->render_response(
					array(
						'success' => false,
						'ability' => $name,
						'ability_found' => true,
						'method_valid' => false,
						'expected_method' => $expected_method,
						'error'   => 'invalid_method',
					),
					$assoc_args
				);
				return;
			}
		}

		$confirmation_required = $this->requires_confirmation( $annotations );
		$confirmation_ok = true;
		if ( $confirmation_required ) {
			$confirmation_ok = $this->has_confirmation( $input );
			if ( ! $confirmation_ok ) {
				$this->render_response(
					array(
						'success' => false,
						'ability' => $name,
						'ability_found' => true,
						'method_valid' => $method_valid,
						'confirmation_required' => true,
						'confirmation_ok' => false,
						'error' => 'confirmation_required',
					),
					$assoc_args
				);
				return;
			}
		}

		$result = $ability->execute( $input );
		if ( is_wp_error( $result ) ) {
			$error_code = $result->get_error_code();
			$permission_ok = null;
			if ( $this->looks_like_permission_error( $error_code ) ) {
				$permission_ok = false;
			}
			$this->render_response(
				array(
					'success' => false,
					'ability' => $name,
					'ability_found' => true,
					'method_valid' => $method_valid,
					'confirmation_required' => $confirmation_required,
					'confirmation_ok' => $confirmation_ok,
					'permission_ok' => $permission_ok,
					'error'   => $result->get_error_code(),
					'message' => $result->get_error_message(),
				),
				$assoc_args
			);
			return;
		}

		if ( is_string( $teardown ) && $teardown !== '' ) {
			$this->sandbox->execute_snippet( $teardown );
		}

		$this->render_response(
			array(
				'success' => true,
				'ability' => $name,
				'ability_found' => true,
				'method_valid' => $method_valid,
				'confirmation_required' => $confirmation_required,
				'confirmation_ok' => $confirmation_ok,
				'permission_ok' => true,
				'result'  => $result,
			),
			$assoc_args
		);
	}

	/**
	 * Render output based on requested format.
	 *
	 * @param array<string, mixed> $response Response payload.
	 * @param array<string, mixed> $assoc_args Command args.
	 */
	private function render_response( array $response, array $assoc_args ): void {
		$format = $assoc_args['format'] ?? 'json';
		if ( 'json' === $format ) {
			WP_CLI::line( wp_json_encode( $response, JSON_PRETTY_PRINT ) );
			return;
		}
		WP_CLI::print_value( $response );
	}

	/**
	 * Check if an ability annotation indicates confirmation is required.
	 *
	 * @param mixed $annotations Ability annotations.
	 * @return bool
	 */
	private function requires_confirmation( $annotations ): bool {
		if ( ! is_array( $annotations ) ) {
			return false;
		}
		if ( ! empty( $annotations['requires_confirmation'] ) ) {
			return true;
		}
		if ( ! empty( $annotations['destructive'] ) ) {
			return true;
		}
		return false;
	}

	/**
	 * Check if an input payload includes a confirmation signal.
	 *
	 * @param mixed $input Ability input payload.
	 * @return bool
	 */
	private function has_confirmation( $input ): bool {
		if ( ! is_array( $input ) ) {
			return false;
		}
		foreach ( [ 'confirm', 'confirmation', 'nonce' ] as $key ) {
			if ( ! empty( $input[ $key ] ) ) {
				return true;
			}
		}
		return false;
	}

	/**
	 * Heuristic for permission-related errors.
	 *
	 * @param string $error_code Error code.
	 * @return bool
	 */
	private function looks_like_permission_error( string $error_code ): bool {
		$code = strtolower( $error_code );
		return str_contains( $code, 'permission' ) || str_contains( $code, 'forbidden' );
	}
}
