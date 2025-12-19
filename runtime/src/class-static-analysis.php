<?php
/**
 * Static code checker.
 *
 * @package WPBench\Runtime
 */

declare(strict_types=1);

namespace WPBench\Runtime;

/**
 * Static code analysis using regex patterns.
 *
 * Checks generated code against required and forbidden patterns
 * to provide objective correctness scoring.
 */
class Static_Analysis {

	/**
	 * Check code against required and forbidden patterns.
	 *
	 * @param string               $code   The generated code to check.
	 * @param array<string, mixed> $checks Configuration with required_patterns and forbidden_patterns.
	 *
	 * @return array{score: float, details: array<string, mixed>}
	 */
	public function check( string $code, array $checks ): array {
		$required_results  = [];
		$forbidden_results = [];
		$total_weight      = 0.0;
		$earned_weight     = 0.0;

		// Check required patterns.
		$required = $checks['required_patterns'] ?? [];
		if ( is_array( $required ) ) {
			foreach ( $required as $pattern_config ) {
				if ( ! is_array( $pattern_config ) ) {
					continue;
				}
				$pattern_value     = $pattern_config['pattern'] ?? '';
				$pattern           = is_string( $pattern_value ) ? $pattern_value : '';
				$description_value = $pattern_config['description'] ?? $pattern;
				$description       = is_string( $description_value ) ? $description_value : $pattern;
				$weight_value      = $pattern_config['weight'] ?? 1.0;
				$weight            = is_numeric( $weight_value ) ? (float) $weight_value : 1.0;

				$total_weight += $weight;
				$found         = $this->safe_preg_match( $pattern, $code );

				$required_results[] = [
					'pattern'     => $pattern,
					'description' => $description,
					'found'       => $found,
					'weight'      => $weight,
				];

				if ( $found ) {
					$earned_weight += $weight;
				}
			}
		}

		// Check forbidden patterns.
		$forbidden = $checks['forbidden_patterns'] ?? [];
		if ( is_array( $forbidden ) ) {
			foreach ( $forbidden as $pattern_config ) {
				if ( ! is_array( $pattern_config ) ) {
					continue;
				}
				$pattern_value     = $pattern_config['pattern'] ?? '';
				$pattern           = is_string( $pattern_value ) ? $pattern_value : '';
				$description_value = $pattern_config['description'] ?? $pattern;
				$description       = is_string( $description_value ) ? $description_value : $pattern;
				$severity_value    = $pattern_config['severity'] ?? 'error';
				$severity          = is_string( $severity_value ) ? $severity_value : 'error';

				$found = $this->safe_preg_match( $pattern, $code );

				$forbidden_results[] = [
					'pattern'     => $pattern,
					'description' => $description,
					'found'       => $found,
					'severity'    => $severity,
				];

				// Forbidden patterns with 'error' severity set score to 0 if found.
				if ( $found && 'error' === $severity ) {
					return [
						'score'   => 0.0,
						'details' => [
							'required'       => $required_results,
							'forbidden'      => $forbidden_results,
							'failure_reason' => "Forbidden pattern found: {$description}",
						],
					];
				}
			}
		}

		// Calculate score from required patterns.
		$score = $total_weight > 0 ? $earned_weight / $total_weight : 1.0;

		return [
			'score'   => round( $score, 4 ),
			'details' => [
				'required'      => $required_results,
				'forbidden'     => $forbidden_results,
				'total_weight'  => $total_weight,
				'earned_weight' => $earned_weight,
			],
		];
	}

	/**
	 * Check if code contains basic PHP syntax errors.
	 *
	 * Uses token_get_all to detect obvious syntax issues.
	 *
	 * @param string $code PHP code to check.
	 *
	 * @return array{valid: bool, error: string|null}
	 */
	public function check_syntax( string $code ): array {
		// Ensure code has opening PHP tag for tokenizer.
		if ( ! preg_match( '/^<\?(?:php)?/i', ltrim( $code ) ) ) {
			$code = '<?php ' . $code;
		}

		// Suppress errors and try to tokenize.
		set_error_handler( fn() => null );

		try {
			// token_get_all will trigger errors on invalid syntax.
			@token_get_all( $code );
			restore_error_handler();

			return [
				'valid' => true,
				'error' => null,
			];

		} catch ( \Throwable $e ) {
			restore_error_handler();
			return [
				'valid' => false,
				'error' => $e->getMessage(),
			];
		}
	}

	/**
	 * Check code for common WordPress security issues.
	 *
	 * @param string $code Code to check.
	 *
	 * @return array{score: float, issues: array<array{type: string, severity: string, description: string}>}
	 */
	public function check_security( string $code ): array {
		$patterns = self::get_security_patterns();

		$issues     = [];
		$score      = 1.0;
		$deductions = 0.0;

		// Check forbidden security patterns.
		foreach ( $patterns['forbidden'] as $pattern_config ) {
			if ( $this->safe_preg_match( $pattern_config['pattern'], $code ) ) {
				$severity = $pattern_config['severity'];
				$issues[] = [
					'type'        => 'security',
					'severity'    => $severity,
					'description' => $pattern_config['description'],
				];

				if ( 'error' === $severity ) {
					$deductions += 0.5;
				} else {
					$deductions += 0.1;
				}
			}
		}

		// Bonus for good security patterns.
		$bonus = 0.0;
		foreach ( $patterns['required'] as $pattern_config ) {
			if ( $this->safe_preg_match( $pattern_config['pattern'], $code ) ) {
				$bonus += 0.05;
			}
		}

		$score = max( 0.0, min( 1.0, 1.0 - $deductions + $bonus ) );

		return [
			'score'  => round( $score, 4 ),
			'issues' => $issues,
		];
	}

	/**
	 * Safely execute preg_match with error handling.
	 *
	 * @param string $pattern Regex pattern.
	 * @param string $subject String to match against.
	 *
	 * @return bool True if pattern matches.
	 */
	private function safe_preg_match( string $pattern, string $subject ): bool {
		// Ensure pattern has delimiters.
		if ( ! preg_match( '/^[\/\#\~\@\!]/', $pattern ) ) {
			$pattern = '/' . $pattern . '/';
		}

		// Suppress regex warnings.
		set_error_handler( fn() => null );

		try {
			$result = preg_match( $pattern, $subject );
			restore_error_handler();
			return 1 === $result;
		} catch ( \Throwable $e ) {
			restore_error_handler();
			return false;
		}
	}

	/**
	 * Get predefined security patterns for WordPress code.
	 *
	 * @return array{forbidden: array<array{pattern: string, description: string, severity: string}>, required: array<array{pattern: string, description: string, weight: float}>}
	 */
	public static function get_security_patterns(): array {
		return [
			'forbidden' => [
				[
					'pattern'     => '/\$_(GET|POST|REQUEST|SERVER|COOKIE)\s*\[[^\]]+\](?!\s*,|\s*\))/s',
					'description' => 'Direct use of superglobals without sanitization',
					'severity'    => 'error',
				],
				[
					'pattern'     => '/\beval\s*\(/i',
					'description' => 'Use of eval() function',
					'severity'    => 'error',
				],
				[
					'pattern'     => '/\bexec\s*\(/i',
					'description' => 'Use of exec() function',
					'severity'    => 'error',
				],
				[
					'pattern'     => '/\bshell_exec\s*\(/i',
					'description' => 'Use of shell_exec() function',
					'severity'    => 'error',
				],
				[
					'pattern'     => '/\bsystem\s*\(/i',
					'description' => 'Use of system() function',
					'severity'    => 'error',
				],
				[
					'pattern'     => '/\bpassthru\s*\(/i',
					'description' => 'Use of passthru() function',
					'severity'    => 'error',
				],
				[
					'pattern'     => '/\b(?:mysql_query|mysqli_query)\s*\([^)]*\$(?!wpdb)/i',
					'description' => 'Direct database queries without $wpdb',
					'severity'    => 'error',
				],
				[
					'pattern'     => '/\bfile_get_contents\s*\(\s*\$/',
					'description' => 'file_get_contents with variable URL (potential SSRF)',
					'severity'    => 'warning',
				],
				[
					'pattern'     => '/echo\s+[^;]*\$[^;]*;(?!.*esc_)/i',
					'description' => 'Echo without escaping',
					'severity'    => 'warning',
				],
			],
			'required'  => [
				[
					'pattern'     => '/\besc_(html|attr|url|js|textarea)\s*\(/i',
					'description' => 'Uses WordPress escaping functions',
					'weight'      => 0.5,
				],
				[
					'pattern'     => '/\bsanitize_(text_field|email|title|key|file_name|user|url)\s*\(/i',
					'description' => 'Uses WordPress sanitization functions',
					'weight'      => 0.5,
				],
				[
					'pattern'     => '/\bwp_nonce_field\s*\(|\bwp_verify_nonce\s*\(/i',
					'description' => 'Uses nonce verification',
					'weight'      => 0.3,
				],
				[
					'pattern'     => '/\bcurrent_user_can\s*\(/i',
					'description' => 'Uses capability checking',
					'weight'      => 0.3,
				],
			],
		];
	}

	/**
	 * Get common WordPress code patterns.
	 *
	 * @return array<string, array<array{pattern: string, description: string}>>
	 */
	public static function get_wordpress_patterns(): array {
		return [
			'hooks'      => [
				[
					'pattern'     => '/\badd_action\s*\(/i',
					'description' => 'Registers an action hook',
				],
				[
					'pattern'     => '/\badd_filter\s*\(/i',
					'description' => 'Registers a filter hook',
				],
				[
					'pattern'     => '/\bdo_action\s*\(/i',
					'description' => 'Triggers an action hook',
				],
				[
					'pattern'     => '/\bapply_filters\s*\(/i',
					'description' => 'Applies filters',
				],
			],
			'queries'    => [
				[
					'pattern'     => '/\bnew\s+WP_Query\s*\(/i',
					'description' => 'Creates WP_Query instance',
				],
				[
					'pattern'     => '/\bget_posts\s*\(/i',
					'description' => 'Uses get_posts()',
				],
				[
					'pattern'     => '/\$wpdb->(?:get_results|get_row|get_var|prepare)\s*\(/i',
					'description' => 'Uses $wpdb methods',
				],
			],
			'shortcodes' => [
				[
					'pattern'     => '/\badd_shortcode\s*\(/i',
					'description' => 'Registers a shortcode',
				],
				[
					'pattern'     => '/\bdo_shortcode\s*\(/i',
					'description' => 'Processes shortcodes',
				],
				[
					'pattern'     => '/\bshortcode_atts\s*\(/i',
					'description' => 'Parses shortcode attributes',
				],
			],
			'rest_api'   => [
				[
					'pattern'     => '/\bregister_rest_route\s*\(/i',
					'description' => 'Registers REST route',
				],
				[
					'pattern'     => '/\bWP_REST_Response\b/i',
					'description' => 'Uses REST response class',
				],
			],
		];
	}
}
