<?php
/**
 * SQLite isolation transport. Run with PHP directly, before WordPress boots.
 *
 * Snapshots travel as base64 on stdout/stdin and are held by the host harness.
 * Temporary files are removed before candidate code can run. VACUUM INTO
 * includes committed WAL contents, unlike copying the live database file.
 */

declare(strict_types=1);

$database = getenv( 'WP_BENCH_SQLITE_PATH' ) ?: '/var/www/html/wp-content/database/.ht.sqlite';
$action   = $argv[1] ?? '';
$temporary = null;
$exit_code = 0;

try {
	$snapshot = null;
	if ( 'prepare' === $action ) {
		// Trusted host-held configuration and database for a brand-new container.
		$state = json_decode( file_get_contents( 'php://stdin' ), true, 512, JSON_THROW_ON_ERROR );
		if ( ! is_string( $state['config'] ?? null ) || ! is_string( $state['database'] ?? null ) ) {
			throw new RuntimeException( 'Invalid isolated runtime baseline.' );
		}
		if ( ! is_dir( dirname( $database ) ) && ! mkdir( dirname( $database ), 0755, true ) ) {
			throw new RuntimeException( 'Could not create SQLite database directory.' );
		}
		if ( strlen( $state['config'] ) !== file_put_contents( '/var/www/html/wp-config.php', $state['config'] ) ) {
			throw new RuntimeException( 'Could not restore WordPress configuration.' );
		}
		$snapshot = $state['database'];
		$action = 'import';
	}
	if ( 'clear' === $action ) {
		foreach ( array( $database, $database . '-wal', $database . '-shm', $database . '-journal' ) as $file ) {
			if ( file_exists( $file ) && ! unlink( $file ) ) {
				throw new RuntimeException( 'Could not remove SQLite database file: ' . $file );
			}
		}
	} elseif ( 'export' === $action || 'import' === $action ) {
		$temporary = tempnam( dirname( $database ), '.wp-bench-snapshot-' );
		if ( false === $temporary ) {
			throw new RuntimeException( 'Could not create a temporary SQLite snapshot.' );
		}
		if ( 'export' === $action ) {
			if ( ! is_file( $database ) ) {
				throw new RuntimeException( 'SQLite database does not exist.' );
			}
			unlink( $temporary ); // VACUUM INTO requires a nonexistent destination.
			$connection = new PDO( 'sqlite:' . $database, null, null, array( PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION ) );
			$connection->exec( 'VACUUM INTO ' . $connection->quote( $temporary ) );
			$connection = null;
			$contents = file_get_contents( $temporary );
			if ( false === $contents ) {
				throw new RuntimeException( 'Could not read SQLite snapshot.' );
			}
			echo base64_encode( $contents );
		} else {
			$contents = base64_decode( trim( $snapshot ?? file_get_contents( 'php://stdin' ) ), true );
			if ( false === $contents || ! str_starts_with( $contents, "SQLite format 3\0" ) ) {
				throw new RuntimeException( 'Invalid SQLite snapshot.' );
			}
			if ( strlen( $contents ) !== file_put_contents( $temporary, $contents ) ) {
				throw new RuntimeException( 'Could not write SQLite snapshot.' );
			}
			$connection = new PDO( 'sqlite:' . $temporary, null, null, array( PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION ) );
			if ( 'ok' !== $connection->query( 'PRAGMA quick_check' )->fetchColumn() ) {
				throw new RuntimeException( 'SQLite snapshot failed integrity validation.' );
			}
			$connection = null;
			// No WordPress connection is open in this process. Discard stale WAL
			// files before atomically replacing the entire database, including
			// candidate-created tables and the adapter's schema metadata.
			foreach ( array( '-wal', '-shm', '-journal' ) as $suffix ) {
				if ( file_exists( $database . $suffix ) && ! unlink( $database . $suffix ) ) {
					throw new RuntimeException( 'Could not remove SQLite journal: ' . $suffix );
				}
			}
			if ( ! rename( $temporary, $database ) ) {
				throw new RuntimeException( 'Could not restore SQLite snapshot.' );
			}
		}
	} else {
		throw new RuntimeException( 'Expected clear, export, import, or prepare.' );
	}
} catch ( Throwable $error ) {
	fwrite( STDERR, $error->getMessage() . PHP_EOL );
	$exit_code = 1;
} finally {
	if ( is_string( $temporary ) && is_file( $temporary ) ) {
		unlink( $temporary );
	}
}
exit( $exit_code );
