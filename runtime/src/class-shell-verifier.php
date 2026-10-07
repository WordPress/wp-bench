<?php
/** Bash execution inside the existing disposable runtime. */
declare(strict_types=1);

namespace WPBench\Runtime;

class Shell_Verifier {
	private const OUTPUT_LIMIT = 1048576;

	/** Run trusted PHP fixtures, a candidate Bash script, and independent assertions. */
	public function verify( array $payload, array $static_checks, array $checks ): array {
		try {
			return $this->verify_script( $payload, $static_checks, $checks );
		} finally {
			// Match the PHP artifact contract even if setup or process creation fails.
			( new Sandbox() )->execute_and_verify( '', array( 'teardown' => $checks['teardown'] ?? '' ) );
		}
	}

	private function verify_script( array $payload, array $static_checks, array $checks ): array {
		$deadline = microtime( true ) + max( 1, (int) ( $payload['timeout_seconds'] ?? 90 ) );
		$code = (string) ( $payload['code'] ?? '' );
		$sandbox = new Sandbox();
		$static = ( new Static_Analysis() )->check( $code, $static_checks );
		$setup = $sandbox->execute_and_verify( '', array(
			'setup' => $checks['setup'] ?? '',
			'assertions' => array( array( 'type' => 'custom_assertion', 'code' => 'return true;', 'weight' => 1 ) ),
		) );
		foreach ( $setup['details']['assertions'] as $assertion ) {
			if ( ! $assertion['passed'] ) {
				return $this->result( $static, $setup, array(), false );
			}
		}
		$runs = array();
		for ( $i = 0; $i < max( 1, (int) ( $checks['repeat'] ?? 1 ) ); ++$i ) {
			$command = $this->execute( $code, max( 0.001, $deadline - microtime( true ) ) );
			$runs[] = $command;
			if ( $command['timed_out'] || $command['output_limit_exceeded'] ) {
				break;
			}
		}
		$GLOBALS['wpbp_shell_result'] = $command;
		// Fixtures were cached in the verifier process before the CLI changed them.
		wp_cache_flush();
		wp_roles()->for_site();
		$runtime = $sandbox->execute_and_verify( '', array(
			'assertions' => $checks['assertions'] ?? array(),
		) );
		$expected_exit = (int) ( $checks['exit_code'] ?? 0 );
		$passed = true;
		foreach ( $runs as $run ) {
			$passed = $passed && ! $run['timed_out'] && ! $run['output_limit_exceeded'] && $run['exit_code'] === $expected_exit;
		}
		$runtime['details']['assertions'][] = array(
			'type' => $passed ? 'shell_exit' : 'execution_error',
			'description' => 'Script finishes within its limits with the expected exit status',
			'passed' => $passed, 'actual' => $command['exit_code'], 'expected' => $expected_exit,
			'weight' => 0,
			'error' => $passed ? null : 'Unexpected exit status, timeout, or output limit exceeded; inspect command.runs.',
		);
		if ( ! $passed ) {
			$runtime['score'] = 0.0;
		}
		$command['runs'] = $runs;
		return $this->result( $static, $runtime, $command, $passed );
	}

	private function execute( string $code, float $timeout ): array {
		$script = tempnam( sys_get_temp_dir(), 'wp-bench-shell-' );
		if ( false === $script ) {
			throw new \RuntimeException( 'Could not create the candidate script.' );
		}
		if ( file_put_contents( $script, $code ) !== strlen( $code ) ) {
			unlink( $script );
			throw new \RuntimeException( 'Could not write the candidate script.' );
		}
		$started = microtime( true );
		$process = null;
		$pipes = array();
		$stdout = '';
		$stderr = '';
		$exit_code = -1;
		$timed_out = false;
		$overflow = false;
		try {
			$process = proc_open( array( 'bash', '--noprofile', '--norc', $script ), array(
				0 => array( 'file', '/dev/null', 'r' ),
				1 => array( 'pipe', 'w' ),
				2 => array( 'pipe', 'w' ),
			), $pipes, ABSPATH );
			if ( ! is_resource( $process ) ) {
				throw new \RuntimeException( 'Could not start Bash.' );
			}
			stream_set_blocking( $pipes[1], false );
			stream_set_blocking( $pipes[2], false );
			while ( true ) {
				$stdout .= stream_get_contents( $pipes[1], 8192 );
				$stderr .= stream_get_contents( $pipes[2], 8192 );
				$overflow = strlen( $stdout ) > self::OUTPUT_LIMIT || strlen( $stderr ) > self::OUTPUT_LIMIT;
				$timed_out = microtime( true ) - $started >= $timeout;
				$status = proc_get_status( $process );
				if ( ! $status['running'] && $status['exitcode'] >= 0 ) {
					$exit_code = $status['exitcode'];
				}
				if ( $overflow || $timed_out ) {
					proc_terminate( $process, 9 );
					break;
				}
				if ( ! $status['running'] && feof( $pipes[1] ) && feof( $pipes[2] ) ) {
					break;
				}
				usleep( 1000 );
			}
		} finally {
			foreach ( $pipes as $pipe ) {
				fclose( $pipe );
			}
			if ( is_resource( $process ) ) {
				proc_terminate( $process, 9 );
				proc_close( $process );
			}
			unlink( $script );
		}
		return array(
			'stdout' => substr( $stdout, 0, self::OUTPUT_LIMIT ),
			'stderr' => substr( $stderr, 0, self::OUTPUT_LIMIT ),
			'exit_code' => $exit_code, 'timed_out' => $timed_out,
			'output_limit_exceeded' => $overflow,
			'duration_ms' => (int) round( ( microtime( true ) - $started ) * 1000 ),
		);
	}

	private function result( array $static, array $runtime, array $command, bool $completed ): array {
		return array(
			'success' => $completed && $runtime['score'] >= 0.999 && $static['score'] >= 0.999,
			'timeout' => ! empty( $command['timed_out'] ),
			'artifact_kind' => 'wp_cli_shell', 'static' => $static, 'runtime' => $runtime,
			'assertions' => $runtime['details']['assertions'], 'command' => $command,
			'version' => Verifier::VERSION,
			'wp_cli_version' => defined( 'WP_CLI_VERSION' ) ? WP_CLI_VERSION : null,
		);
	}
}
