<?php
/**
 * Runtime execution environment.
 *
 * @package WPBench\Runtime
 */

declare(strict_types=1);

namespace WPBench\Runtime;

/**
 * Manages runtime execution environment for code testing.
 *
 * Safety is provided by Docker isolation (wp-env container).
 * Uses try/catch and shutdown handlers to capture errors.
 */
class Sandbox {

	/**
	 * Last error captured by shutdown handler.
	 *
	 * @var array{type: int, message: string, file: string, line: int}|null
	 */
	private static ?array $last_fatal_error = null;

	/**
	 * Execute generated code and verify against assertions.
	 *
	 * @param string               $code   The generated code to execute.
	 * @param array<string, mixed> $checks Runtime check configuration.
	 *
	 * @return array{score: float, details: array<string, mixed>}
	 */
	public function execute_and_verify( string $code, array $checks ): array {
		$setup_value    = $checks['setup'] ?? '';
		$setup          = is_string( $setup_value ) ? $setup_value : '';
		$teardown_value = $checks['teardown'] ?? '';
		$teardown       = is_string( $teardown_value ) ? $teardown_value : '';
		$assertions     = $checks['assertions'] ?? [];

		$results       = [];
		$total_weight  = 0.0;
		$passed_weight = 0.0;

		// Store original error handler.
		$original_error_handler = set_error_handler( [ $this, 'handle_error' ] );

		// Register shutdown handler for fatal errors.
		self::$last_fatal_error = null;
		register_shutdown_function( [ $this, 'handle_shutdown' ] );

		try {
			// Run setup code if provided.
			if ( ! empty( $setup ) ) {
				$this->safe_eval( $setup );
			}

			// Execute the generated code.
			$this->safe_eval( $code );

			// Run assertions.
			if ( is_array( $assertions ) ) {
				foreach ( $assertions as $assertion ) {
					if ( ! is_array( $assertion ) ) {
						continue;
					}
					$weight_value  = $assertion['weight'] ?? 1.0;
					$weight        = is_numeric( $weight_value ) ? (float) $weight_value : 1.0;
					$total_weight += $weight;

					$assertion_result = $this->run_assertion( $assertion );
					$results[]        = $assertion_result;

					if ( $assertion_result['passed'] ) {
						$passed_weight += $weight;
					}
				}
			}
		} catch ( \Throwable $e ) {
			$results[] = [
				'type'        => 'execution_error',
				'description' => 'Code execution failed',
				'passed'      => false,
				'error'       => $e->getMessage(),
				'trace'       => $this->get_safe_trace( $e ),
			];

		} finally {
			// Run teardown.
			if ( ! empty( $teardown ) ) {
				try {
					$this->safe_eval( $teardown );
				} catch ( \Throwable $e ) {
					// Teardown errors are intentionally ignored to not mask actual test failures.
					unset( $e );
				}
			}

			// Restore error handler.
			restore_error_handler();
		}

		// Check for fatal errors captured by shutdown handler.
		if ( null !== self::$last_fatal_error ) {
			$results[] = [
				'type'        => 'fatal_error',
				'description' => 'Fatal error during execution',
				'passed'      => false,
				'error'       => self::$last_fatal_error['message'],
			];
		}

		$score = $total_weight > 0 ? $passed_weight / $total_weight : 0.0;

		return [
			'score'   => round( $score, 4 ),
			'details' => [
				'assertions'    => $results,
				'total_weight'  => $total_weight,
				'passed_weight' => $passed_weight,
			],
		];
	}

	/**
	 * Run a single assertion.
	 *
	 * @param array<string, mixed> $assertion Assertion configuration.
	 *
	 * @return array<string, mixed> Assertion result.
	 */
	private function run_assertion( array $assertion ): array {
		$type_value        = $assertion['type'] ?? '';
		$type              = is_string( $type_value ) ? $type_value : '';
		$target_value      = $assertion['target'] ?? null;
		$target            = is_string( $target_value ) ? $target_value : '';
		$expected          = $assertion['expected'] ?? null;
		$description_value = $assertion['description'] ?? $type;
		$description       = is_string( $description_value ) ? $description_value : $type;
		$expected_str      = is_string( $expected ) ? $expected : '';

		$result = [
			'type'        => $type,
			'description' => $description,
			'passed'      => false,
			'actual'      => null,
			'expected'    => $expected,
		];

		try {
				$result = match ( $type ) {
					'function_exists'     => $this->assert_function_exists( $target, $result ),
					'class_exists'        => $this->assert_class_exists( $target, $result ),
					'shortcode_exists'    => $this->assert_shortcode_exists( $target, $result ),
					'hook_registered'     => $this->assert_hook_registered( $target, $assertion, $result ),
					'output_contains'     => $this->assert_output_contains( $target, $expected_str, $result ),
					'output_not_contains' => $this->assert_output_not_contains( $target, $expected_str, $result ),
					'output_equals'       => $this->assert_output_equals( $target, $expected_str, $result ),
					'output_matches'      => $this->assert_output_matches( $target, $expected_str, $result ),
					'rest_response'       => $this->assert_rest_response( $target, $assertion, $result ),
					'returns_value'       => $this->assert_returns_value( $target, $expected, $result ),
					'query_result'        => $this->assert_query_result( $target, $expected, $result ),
					'option_value'        => $this->assert_option_value( $target, $expected, $result ),
					'post_meta_value'     => $this->assert_post_meta_value( $assertion, $result ),
					'custom_assertion'    => $this->assert_custom( $assertion, $result ),
					default               => array_merge( $result, [ 'error' => "Unknown assertion type: {$type}" ] ),
				};
		} catch ( \Throwable $e ) {
			$result['error'] = $e->getMessage();
		}

		return $result;
	}

	/**
	 * Assert that a function exists.
	 *
	 * @param string               $function_name Function name.
	 * @param array<string, mixed> $result        Base result array.
	 *
	 * @return array<string, mixed>
	 */
	private function assert_function_exists( string $function_name, array $result ): array {
		$result['passed'] = function_exists( $function_name );
		$result['actual'] = $result['passed'] ? 'exists' : 'not found';
		return $result;
	}

	/**
	 * Assert that a class exists.
	 *
	 * @param string               $class_name Class name.
	 * @param array<string, mixed> $result     Base result array.
	 *
	 * @return array<string, mixed>
	 */
	private function assert_class_exists( string $class_name, array $result ): array {
		$result['passed'] = class_exists( $class_name );
		$result['actual'] = $result['passed'] ? 'exists' : 'not found';
		return $result;
	}

	/**
	 * Assert that a shortcode is registered.
	 *
	 * @param string               $tag    Shortcode tag.
	 * @param array<string, mixed> $result Base result array.
	 *
	 * @return array<string, mixed>
	 */
	private function assert_shortcode_exists( string $tag, array $result ): array {
		global $shortcode_tags;
		$result['passed'] = isset( $shortcode_tags[ $tag ] );
		$result['actual'] = $result['passed'] ? 'registered' : 'not registered';
		return $result;
	}

	/**
	 * Assert that a hook has callbacks registered.
	 *
	 * @param string               $hook      Hook name.
	 * @param array<string, mixed> $assertion Full assertion config (may include callback).
	 * @param array<string, mixed> $result    Base result array.
	 *
	 * @return array<string, mixed>
	 */
	private function assert_hook_registered( string $hook, array $assertion, array $result ): array {
		global $wp_filter;
		$callback_value = $assertion['callback'] ?? null;
		$callback       = is_string( $callback_value ) ? $callback_value : null;

		if ( ! isset( $wp_filter[ $hook ] ) ) {
			$result['actual'] = 'hook not found';
			return $result;
		}

		if ( null === $callback ) {
			$result['passed'] = true;
			$result['actual'] = 'hook has callbacks';
			return $result;
		}

		// Check for specific callback.
		foreach ( $wp_filter[ $hook ]->callbacks as $priority => $callbacks ) {
			foreach ( $callbacks as $cb ) {
				if ( $this->callback_matches( $cb['function'], $callback ) ) {
					$result['passed'] = true;
					$result['actual'] = 'callback registered';
					return $result;
				}
			}
		}

		$result['actual'] = 'callback not found';
		return $result;
	}

	/**
	 * Assert that code output contains a string.
	 *
	 * @param string               $code_or_callable Code to execute or callable reference.
	 * @param string               $expected         Expected substring.
	 * @param array<string, mixed> $result           Base result array.
	 *
	 * @return array<string, mixed>
	 */
	private function assert_output_contains( string $code_or_callable, string $expected, array $result ): array {
		ob_start();
		$this->safe_eval( $code_or_callable );
		$output = ob_get_clean();
		$output = false !== $output ? $output : '';

		$result['actual'] = $output;
		$result['passed'] = str_contains( $output, $expected );
		return $result;
	}

	/**
	 * Assert that code output does not contain a string.
	 *
	 * @param string               $code_or_callable Code to execute.
	 * @param string               $expected         Forbidden substring.
	 * @param array<string, mixed> $result           Base result array.
	 *
	 * @return array<string, mixed>
	 */
	private function assert_output_not_contains( string $code_or_callable, string $expected, array $result ): array {
		ob_start();
		$this->safe_eval( $code_or_callable );
		$output = ob_get_clean();
		$output = false !== $output ? $output : '';

		$result['actual'] = $output;
		$result['passed'] = ! str_contains( $output, $expected );
		return $result;
	}

	/**
	 * Assert that code output equals a string.
	 *
	 * @param string               $code_or_callable Code to execute.
	 * @param string               $expected         Expected output.
	 * @param array<string, mixed> $result           Base result array.
	 *
	 * @return array<string, mixed>
	 */
	private function assert_output_equals( string $code_or_callable, string $expected, array $result ): array {
		ob_start();
		$this->safe_eval( $code_or_callable );
		$output = ob_get_clean();
		$output = false !== $output ? $output : '';

		$result['actual'] = $output;
		$result['passed'] = trim( $expected ) === trim( $output );
		return $result;
	}

	/**
	 * Assert that code output matches a regex pattern.
	 *
	 * @param string               $code_or_callable Code to execute.
	 * @param string               $pattern          Regex pattern.
	 * @param array<string, mixed> $result           Base result array.
	 *
	 * @return array<string, mixed>
	 */
	private function assert_output_matches( string $code_or_callable, string $pattern, array $result ): array {
		ob_start();
		$this->safe_eval( $code_or_callable );
		$output = ob_get_clean();
		$output = false !== $output ? $output : '';

		$result['actual'] = $output;

		// Ensure pattern has delimiters.
		if ( ! preg_match( '/^[\/\#\~\@]/', $pattern ) ) {
			$pattern = '/' . $pattern . '/';
		}

		$result['passed'] = (bool) preg_match( $pattern, $output );
		return $result;
	}

	/**
	 * Assert a REST API response by dispatching through WordPress internals.
	 *
	 * @param string               $route     REST route, e.g. /demo/v1/item.
	 * @param array<string, mixed> $assertion Full assertion config.
	 * @param array<string, mixed> $result    Base result array.
	 *
	 * @return array<string, mixed>
	 */
	private function assert_rest_response( string $route, array $assertion, array $result ): array {
		if ( ! did_action( 'rest_api_init' ) ) {
			do_action( 'rest_api_init' );
		}

		$method_value = $assertion['method'] ?? 'GET';
		$method       = is_string( $method_value ) ? strtoupper( $method_value ) : 'GET';
		$request      = new \WP_REST_Request( $method, $route );

		$params = $assertion['params'] ?? [];
		if ( is_array( $params ) ) {
			foreach ( $params as $key => $value ) {
				if ( is_string( $key ) ) {
					$request->set_param( $key, $value );
				}
			}
		}

		$headers = $assertion['headers'] ?? [];
		if ( is_array( $headers ) ) {
			foreach ( $headers as $key => $value ) {
				if ( is_string( $key ) && is_string( $value ) ) {
					$request->set_header( $key, $value );
				}
			}
		}

		if ( array_key_exists( 'body', $assertion ) ) {
			$request->set_body( wp_json_encode( $assertion['body'] ) );
			$request->set_header( 'content-type', 'application/json' );
		}

		$response = rest_do_request( $request );
		$server   = rest_get_server();
		$data     = $server->response_to_data( $response, false );
		$status   = $response->get_status();

		$result['actual'] = [
			'status' => $status,
			'data'   => $data,
		];

		$expected_status = $assertion['expected_status'] ?? null;
		if ( is_numeric( $expected_status ) && (int) $expected_status !== $status ) {
			$result['expected'] = [ 'status' => (int) $expected_status ];
			return $result;
		}

		if ( array_key_exists( 'expected_data', $assertion ) ) {
			$result['expected'] = $assertion['expected_data'];
			// phpcs:ignore Universal.Operators.StrictComparisons.LooseEqual -- REST data can contain arrays/objects.
			$result['passed'] = $assertion['expected_data'] == $data;
			return $result;
		}

		$encoded = wp_json_encode( $data );
		$encoded = is_string( $encoded ) ? $encoded : '';

		$contains = $assertion['body_contains'] ?? null;
		if ( is_string( $contains ) && ! str_contains( $encoded, $contains ) ) {
			$result['expected'] = [ 'body_contains' => $contains ];
			return $result;
		}

		$not_contains = $assertion['body_not_contains'] ?? null;
		if ( is_string( $not_contains ) && str_contains( $encoded, $not_contains ) ) {
			$result['expected'] = [ 'body_not_contains' => $not_contains ];
			return $result;
		}

		$result['passed'] = true;
		return $result;
	}

	/**
	 * Assert that code returns a specific value.
	 *
	 * @param string               $code_or_callable Code to execute.
	 * @param mixed                $expected         Expected return value.
	 * @param array<string, mixed> $result           Base result array.
	 *
	 * @return array<string, mixed>
	 */
	private function assert_returns_value( string $code_or_callable, mixed $expected, array $result ): array {
		$actual = $this->safe_eval( $code_or_callable );

		$result['actual'] = $actual;
		$result['passed'] = $expected === $actual;
		return $result;
	}

	/**
	 * Assert database query result.
	 *
	 * @param string               $query    SQL query.
	 * @param mixed                $expected Expected result.
	 * @param array<string, mixed> $result   Base result array.
	 *
	 * @return array<string, mixed>
	 */
	private function assert_query_result( string $query, mixed $expected, array $result ): array {
		global $wpdb;
		// phpcs:ignore WordPress.DB.DirectDatabaseQuery.DirectQuery, WordPress.DB.DirectDatabaseQuery.NoCaching, WordPress.DB.PreparedSQL.NotPrepared -- Query is from test config.
		$actual = $wpdb->get_results( $query );

		$result['actual'] = $actual;
		// phpcs:ignore Universal.Operators.StrictComparisons.LooseEqual -- Loose comparison needed for objects/arrays.
		$result['passed'] = $expected == $actual;
		return $result;
	}

	/**
	 * Assert WordPress option value.
	 *
	 * @param string               $option   Option name.
	 * @param mixed                $expected Expected value.
	 * @param array<string, mixed> $result   Base result array.
	 *
	 * @return array<string, mixed>
	 */
	private function assert_option_value( string $option, mixed $expected, array $result ): array {
		$actual = get_option( $option );

		$result['actual'] = $actual;
		$result['passed'] = $expected === $actual;
		return $result;
	}

	/**
	 * Assert post meta value.
	 *
	 * @param array<string, mixed> $assertion Assertion config with post_id and meta_key.
	 * @param array<string, mixed> $result    Base result array.
	 *
	 * @return array<string, mixed>
	 */
	private function assert_post_meta_value( array $assertion, array $result ): array {
		$post_id_value  = $assertion['post_id'] ?? 0;
		$post_id        = is_int( $post_id_value ) ? $post_id_value : 0;
		$meta_key_value = $assertion['meta_key'] ?? '';
		$meta_key       = is_string( $meta_key_value ) ? $meta_key_value : '';
		$expected       = $assertion['expected'] ?? null;

		$actual = get_post_meta( $post_id, $meta_key, true );

		$result['actual'] = $actual;
		$result['passed'] = $expected === $actual;
		return $result;
	}

	/**
	 * Run a custom assertion via code.
	 *
	 * @param array<string, mixed> $assertion Assertion config with 'code' key.
	 * @param array<string, mixed> $result    Base result array.
	 *
	 * @return array<string, mixed>
	 */
	private function assert_custom( array $assertion, array $result ): array {
		$code_value = $assertion['code'] ?? 'return false;';
		$code       = is_string( $code_value ) ? $code_value : 'return false;';
		$actual     = $this->safe_eval( $code );

		$result['actual'] = $actual;
		$result['passed'] = (bool) $actual;
		return $result;
	}

	/**
	 * Safely evaluate PHP code with error handling.
	 *
	 * @param string $code PHP code to execute.
	 *
	 * @return mixed Evaluation result.
	 */
	private function safe_eval( string $code ): mixed {
		// Strip opening PHP tag if present.
		$code = preg_replace( '/^<\?php\s*/i', '', $code );

		// phpcs:ignore Squiz.PHP.Eval.Discouraged -- Required for runtime code verification in benchmarks.
		return eval( $code );
	}

	/**
	 * Check if a registered callback matches an expected callback string.
	 *
	 * @param mixed  $registered Registered callback.
	 * @param string $expected   Expected callback string.
	 *
	 * @return bool
	 */
	private function callback_matches( mixed $registered, string $expected ): bool {
		if ( is_string( $registered ) ) {
			return $expected === $registered;
		}

		if ( is_array( $registered ) && 2 === count( $registered ) ) {
			$class = is_object( $registered[0] )
				? get_class( $registered[0] )
				: $registered[0];
			return $expected === "{$class}::{$registered[1]}"
				|| $expected === $registered[1];
		}

		return false;
	}

	/**
	 * Get a safe stack trace (limited depth, no sensitive data).
	 *
	 * @param \Throwable $e Exception.
	 *
	 * @return string
	 */
	private function get_safe_trace( \Throwable $e ): string {
		$trace = $e->getTraceAsString();

		// Limit to first 5 lines.
		$lines = explode( "\n", $trace );
		$lines = array_slice( $lines, 0, 5 );

		return implode( "\n", $lines );
	}

	/**
	 * Error handler for recoverable errors.
	 *
	 * @param int    $errno   Error number.
	 * @param string $errstr  Error message.
	 * @param string $errfile Error file.
	 * @param int    $errline Error line.
	 *
	 * @return never Function always throws.
	 *
	 * @throws \ErrorException Always thrown to convert errors to exceptions.
	 */
	public function handle_error( int $errno, string $errstr, string $errfile = '', int $errline = 0 ): never {
		// phpcs:ignore WordPress.Security.EscapeOutput.ExceptionNotEscaped -- ErrorException params, not output.
		throw new \ErrorException( $errstr, 0, $errno, $errfile, $errline );
	}

	/**
	 * Shutdown handler for fatal errors.
	 */
	public function handle_shutdown(): void {
		$error = error_get_last();

		if ( null !== $error && in_array( $error['type'], [ E_ERROR, E_PARSE, E_CORE_ERROR, E_COMPILE_ERROR ], true ) ) {
			self::$last_fatal_error = $error;
		}
	}
}
