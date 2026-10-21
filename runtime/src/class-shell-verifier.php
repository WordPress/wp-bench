<?php
/** Bash execution inside the existing disposable runtime. */
declare(strict_types=1);

namespace WPBench\Runtime;

class Shell_Verifier {
	private const OUTPUT_LIMIT = 1048576;
	/** WP-CLI commands that run candidate PHP or SQL instead of native operations. */
	private const DISABLED_COMMANDS = array( 'eval', 'eval-file', 'shell', 'db query', 'db cli', 'db import', 'package' );
	/** Files that make WordPress or WP-CLI load candidate PHP or config. */
	private const WATCHED_FILE = '/(\.php|^\.maintenance|^wp-cli(\.local)?\.yml)$/';

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
		$config = tempnam( sys_get_temp_dir(), 'wp-bench-cli-config-' );
		if ( false === $config ) {
			throw new \RuntimeException( 'Could not create the WP-CLI config.' );
		}
		try {
			$env = $this->guard_env( $config ) + getenv();
			$since = time();
			$before = $this->snapshot( $since, $config );
			for ( $i = 0; $i < max( 1, (int) ( $checks['repeat'] ?? 1 ) ); ++$i ) {
				$command = $this->execute( $code, max( 0.001, $deadline - microtime( true ) ), $env );
				$runs[] = $command;
				if ( $command['timed_out'] || $command['output_limit_exceeded'] ) {
					break;
				}
			}
			// Deleting files cannot run PHP; new or rewritten ones can, even if removed again.
			$loaded = is_file( $config . '.loaded' ) ? file( $config . '.loaded', FILE_IGNORE_NEW_LINES ) : array();
			$loaded = array_filter( $loaded, static fn( $path ) => ! isset( $before[ $path ] )
				&& ( str_starts_with( $path, ABSPATH ) || str_starts_with( $path, sys_get_temp_dir() . '/' ) ) );
			$after = $this->snapshot( $since, $config );
			$planted = array_values( array_unique( array_merge( array_keys( array_diff_assoc( $after, $before ) ), $loaded ) ) );
		} finally {
			array_map( 'unlink', glob( $config . '*' ) );
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
		$runtime['details']['assertions'][] = array(
			'type' => $planted ? 'execution_error' : 'shell_policy',
			'description' => 'Script leaves no new or changed PHP files or WP-CLI config',
			'passed' => ! $planted, 'actual' => $planted, 'expected' => array(),
			'weight' => 0,
			'error' => $planted ? 'The script planted code WordPress or WP-CLI would load: ' . implode( ', ', $planted ) : null,
		);
		$passed = $passed && ! $planted;
		if ( ! $passed ) {
			$runtime['score'] = 0.0;
		}
		$command['runs'] = $runs;
		return $this->result( $static, $runtime, $command, $passed );
	}

	/**
	 * Write a WP-CLI config that disables the commands running candidate PHP or SQL
	 * and requires record-loaded-files.php, which logs what each `wp` call loaded.
	 *
	 * Docker mounts the writable tmpfs paths noexec by default, so `wp` becomes an exported Bash
	 * function instead of a wrapper file. It pins this config and rejects --exec and
	 * --require, which WP-CLI runs before any config `require` could stop them.
	 * The config is also exported, so `command wp` or xargs still get disabled_commands;
	 * their flags, and the php binary itself, are left to the static policy.
	 */
	private function guard_env( string $config ): array {
		$yaml = sprintf(
			"disabled_commands:\n  - %s\nrequire:\n  - %s\n",
			implode( "\n  - ", self::DISABLED_COMMANDS ), dirname( __DIR__ ) . '/record-loaded-files.php'
		);
		if ( file_put_contents( $config, $yaml ) !== strlen( $yaml ) ) {
			throw new \RuntimeException( 'Could not write the WP-CLI config.' );
		}
		return array(
			'WP_CLI_CONFIG_PATH' => $config,
			'BASH_FUNC_wp%%' => sprintf( <<<'BASH'
() {
	local arg
	for arg in "$@"; do
		case "$arg" in
		--exec|--exec=*|--require|--require=*)
			echo "Error: ${arg%%%%=*} is disabled in wp-bench; use native WP-CLI commands." >&2
			return 1 ;;
		esac
	done
	WP_CLI_CONFIG_PATH=%s command wp "$@"
}
BASH, escapeshellarg( $config ) ),
		);
	}

	/** Identity of every file that could load candidate PHP in the writable WordPress root and temp dir. */
	private function snapshot( int $since, string $config ): array {
		// The guard config is watched too: emptying it would turn the guard off.
		$files = array( $config => $this->identity( $config, $since ) );
		foreach ( array( ABSPATH, sys_get_temp_dir() ) as $root ) {
			$iterator = new \RecursiveIteratorIterator( new \RecursiveDirectoryIterator( $root, \FilesystemIterator::SKIP_DOTS ) );
			foreach ( $iterator as $file ) {
				if ( preg_match( self::WATCHED_FILE, $file->getFilename() ) ) {
					$files[ $file->getPathname() ] = $this->identity( $file->getPathname(), $since );
				}
			}
		}
		// WP-CLI also reads project config from every parent of the working directory.
		for ( $dir = dirname( rtrim( ABSPATH, '/' ) ); ; $dir = dirname( $dir ) ) {
			foreach ( array( 'wp-cli.yml', 'wp-cli.local.yml' ) as $name ) {
				if ( file_exists( "$dir/$name" ) ) {
					$files[ "$dir/$name" ] = $this->identity( "$dir/$name", $since );
				}
			}
			if ( '/' === $dir ) {
				break;
			}
		}
		return $files;
	}

	/**
	 * ctime and inode change on any write or replacement and cannot be set back; a missing file differs too.
	 * ctime has one-second resolution, so files touched since the first snapshot are also hashed.
	 */
	private function identity( string $path, int $since ): string {
		clearstatcache( true, $path );
		$ctime = @filectime( $path );
		return @fileinode( $path ) . ':' . $ctime . ( $ctime >= $since ? ':' . md5_file( $path ) : '' );
	}

	private function execute( string $code, float $timeout, array $env ): array {
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
			), $pipes, ABSPATH, $env );
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
