<?php
/**
 * Candidate plugin artifact installer.
 *
 * @package WPBench\Runtime
 */

declare(strict_types=1);

namespace WPBench\Runtime;

/**
 * Writes candidate plugin files into wp-content/plugins, loads them for
 * verification, and removes them afterwards.
 *
 * Path safety: file paths are validated against traversal and absolute
 * paths before any write, and all writes are confined to a dedicated
 * wp-bench-candidate directory that is deleted on cleanup.
 */
class Artifact_Installer {

	private const PLUGIN_DIR_PREFIX = 'wp-bench-candidate';

	/**
	 * Directory created for the current candidate, if any.
	 */
	private ?string $plugin_dir = null;

	/**
	 * Install candidate plugin files and include the main plugin file.
	 *
	 * @param array<string, string> $files Relative path => file contents.
	 * @return array{success: bool, error?: string, plugin_dir?: string}
	 */
	public function install( array $files ): array {
		$base = trailingslashit( WP_PLUGIN_DIR ) . self::PLUGIN_DIR_PREFIX . '-' . wp_generate_password( 8, false );

		foreach ( $files as $relative_path => $contents ) {
			if ( ! is_string( $relative_path ) || ! is_string( $contents ) ) {
				return [
					'success' => false,
					'error'   => 'Artifact file entries must map string paths to string contents.',
				];
			}
			$validated = $this->validate_relative_path( $relative_path );
			if ( null === $validated ) {
				return [
					'success' => false,
					'error'   => "Unsafe artifact file path: {$relative_path}",
				];
			}
		}

		if ( ! wp_mkdir_p( $base ) ) {
			return [
				'success' => false,
				'error'   => 'Could not create candidate plugin directory.',
			];
		}
		$this->plugin_dir = $base;

		foreach ( $files as $relative_path => $contents ) {
			$validated = $this->validate_relative_path( $relative_path );
			$target    = $base . '/' . $validated;
			$dir       = dirname( $target );
			if ( ! wp_mkdir_p( $dir ) ) {
				return [
					'success' => false,
					'error'   => "Could not create directory for: {$relative_path}",
				];
			}
			// phpcs:ignore WordPress.WP.AlternativeFunctions.file_system_operations_file_put_contents -- Runtime container filesystem.
			if ( false === file_put_contents( $target, $contents ) ) {
				return [
					'success' => false,
					'error'   => "Could not write artifact file: {$relative_path}",
				];
			}
		}

		$main_file = $this->find_main_plugin_file( $files );
		if ( null === $main_file ) {
			return [
				'success' => false,
				'error'   => 'No top-level plugin file with a Plugin Name header found.',
			];
		}

		try {
			self::include_at_global_scope( $base . '/' . $main_file );
		} catch ( \Throwable $e ) {
			return [
				'success' => false,
				'error'   => 'Loading candidate plugin failed: ' . $e->getMessage(),
			];
		}

		return [
			'success'    => true,
			'plugin_dir' => $base,
		];
	}

	/**
	 * Remove the candidate plugin directory.
	 */
	public function cleanup(): void {
		if ( null === $this->plugin_dir || ! is_dir( $this->plugin_dir ) ) {
			return;
		}
		$this->delete_dir( $this->plugin_dir );
		$this->plugin_dir = null;
	}

	/**
	 * Validate a relative artifact path; null when unsafe.
	 */
	private function validate_relative_path( string $path ): ?string {
		$normalized = str_replace( '\\', '/', trim( $path ) );
		if ( '' === $normalized || str_starts_with( $normalized, '/' ) || 1 === preg_match( '/^[A-Za-z]:/', $normalized ) ) {
			return null;
		}
		foreach ( explode( '/', $normalized ) as $segment ) {
			if ( '' === $segment || '.' === $segment || '..' === $segment ) {
				return null;
			}
			if ( 1 !== preg_match( '/^[A-Za-z0-9._-]+$/', $segment ) ) {
				return null;
			}
		}
		return $normalized;
	}

	/**
	 * Include the plugin's main file the way wp-settings.php does: at global scope.
	 *
	 * Core includes active plugins from the top level of wp-settings.php, so any
	 * variable a plugin assigns at file scope is a global that its callbacks can
	 * read with `global $name`. Including from inside a method would make those
	 * variables method-local and silently break that (common) pattern, so every
	 * variable the file defines is promoted to $GLOBALS after the include.
	 *
	 * @param string $file Absolute path of the main plugin file.
	 */
	private static function include_at_global_scope( string $file ): void {
		include_once $file;

		foreach ( get_defined_vars() as $wpbp_name => $wpbp_value ) {
			if ( 'file' === $wpbp_name ) {
				continue;
			}
			$GLOBALS[ $wpbp_name ] = $wpbp_value;
		}
	}

	/**
	 * Locate the top-level PHP file carrying the plugin header.
	 *
	 * @param array<string, string> $files Relative path => contents.
	 */
	private function find_main_plugin_file( array $files ): ?string {
		foreach ( $files as $path => $contents ) {
			if ( str_contains( $path, '/' ) || ! str_ends_with( $path, '.php' ) ) {
				continue;
			}
			if ( 1 === preg_match( '/Plugin\s+Name\s*:/i', $contents ) ) {
				return $path;
			}
		}
		return null;
	}

	/**
	 * Recursively delete a directory.
	 */
	private function delete_dir( string $dir ): void {
		$items = scandir( $dir );
		if ( false === $items ) {
			return;
		}
		foreach ( $items as $item ) {
			if ( '.' === $item || '..' === $item ) {
				continue;
			}
			$path = $dir . '/' . $item;
			if ( is_dir( $path ) ) {
				$this->delete_dir( $path );
			} else {
				// phpcs:ignore WordPress.WP.AlternativeFunctions.unlink_unlink -- Runtime container filesystem.
				unlink( $path );
			}
		}
		// phpcs:ignore WordPress.WP.AlternativeFunctions.rmdir_rmdir -- Runtime container filesystem.
		rmdir( $dir );
	}
}
