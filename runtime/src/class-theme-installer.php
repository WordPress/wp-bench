<?php
/**
 * Candidate theme artifact installer.
 *
 * @package WPBench\Runtime
 */

declare(strict_types=1);

namespace WPBench\Runtime;

/**
 * Writes candidate theme files into wp-content/themes, activates the theme
 * the way wp-settings.php boots one, and restores the previous theme after
 * verification.
 *
 * Installing and activating are separate steps so a test's setup code can
 * run in between, for example to install a fixture parent theme that the
 * candidate child theme names in its Template header.
 */
class Theme_Installer extends Artifact_Installer {

	private const THEME_DIR_PREFIX = 'wp-bench-candidate';

	/**
	 * Actions that have not fired yet while wp-settings.php loads a theme.
	 */
	private const LATER_ACTIONS = [ 'after_setup_theme', 'init', 'widgets_init', 'wp_loaded' ];

	/**
	 * Directory name (stylesheet) of the installed candidate theme.
	 */
	private ?string $slug = null;

	/**
	 * Theme options to restore on cleanup, once the theme was activated.
	 *
	 * @var array<string, mixed>|null
	 */
	private ?array $previous = null;

	/**
	 * Write candidate theme files without activating the theme.
	 *
	 * @param array<string, string> $files Relative path => file contents.
	 * @return array{success: bool, error?: string, theme_dir?: string}
	 */
	public function install( array $files ): array {
		$stylesheet = $files['style.css'] ?? null;
		if ( ! is_string( $stylesheet ) || 1 !== preg_match( '/Theme\s+Name\s*:/i', $stylesheet ) ) {
			return [
				'success' => false,
				'error'   => 'No top-level style.css with a Theme Name header found.',
			];
		}

		$slug  = self::THEME_DIR_PREFIX . '-' . strtolower( wp_generate_password( 8, false ) );
		$base  = trailingslashit( get_theme_root() ) . $slug;
		$error = $this->write_files( $base, $files, 'theme' );
		if ( null !== $error ) {
			return [
				'success' => false,
				'error'   => $error,
			];
		}
		$this->slug = $slug;

		return [
			'success'   => true,
			'theme_dir' => $base,
		];
	}

	/**
	 * Make the candidate the active theme and load it like wp-settings.php.
	 *
	 * The site has already booted with another theme, so that theme's hooks
	 * are removed first and theme supports start empty. Then the stylesheet
	 * and template options switch, the theme functions.php files load (child
	 * first, then parent) and after_setup_theme fires, while did_action()
	 * reports after_setup_theme, init, widgets_init and wp_loaded as not
	 * fired yet, as it does during a real boot.
	 *
	 * @throws \RuntimeException When the candidate theme is missing or broken.
	 */
	public function activate(): void {
		if ( null === $this->slug ) {
			throw new \RuntimeException( 'No candidate theme was installed.' );
		}

		wp_clean_themes_cache();
		$theme = wp_get_theme( $this->slug );
		if ( ! $theme->exists() ) {
			throw new \RuntimeException( 'Candidate theme could not be found.' );
		}
		$errors = $theme->errors();
		if ( is_wp_error( $errors ) ) {
			throw new \RuntimeException( 'Candidate theme is broken: ' . $errors->get_error_message() );
		}

		$this->previous = [
			'template'      => get_option( 'template' ),
			'stylesheet'    => get_option( 'stylesheet' ),
			'current_theme' => get_option( 'current_theme' ),
		];
		$this->unhook_theme_files( array_unique( [ get_stylesheet_directory(), get_template_directory() ] ) );

		update_option( 'template', $theme->get_template() );
		update_option( 'stylesheet', $theme->get_stylesheet() );
		update_option( 'current_theme', $theme->get( 'Name' ) );
		self::refresh_theme_state();

		// Theme features and editor styles belong to the theme that set them.
		$GLOBALS['_wp_theme_features'] = [];
		$GLOBALS['editor_styles']      = [];
		unset( $GLOBALS['content_width'] );

		// Core checks did_action() during theme setup, for example
		// add_theme_support( 'title-tag' ) after wp_loaded is flagged as wrong.
		// Report the counts a real boot has at this point, then restore them.
		global $wp_actions;
		$fired = [];
		foreach ( self::LATER_ACTIONS as $action ) {
			$fired[ $action ] = $wp_actions[ $action ] ?? null;
			unset( $wp_actions[ $action ] );
		}

		try {
			foreach ( wp_get_active_and_valid_themes() as $theme_path ) {
				wp_get_theme( basename( $theme_path ) )->load_textdomain();
				if ( file_exists( $theme_path . '/functions.php' ) ) {
					self::include_theme_functions( $theme_path . '/functions.php' );
				}
			}

			do_action( 'after_setup_theme' );
		} finally {
			foreach ( $fired as $action => $count ) {
				if ( null === $count ) {
					unset( $wp_actions[ $action ] );
				} else {
					$wp_actions[ $action ] = $count;
				}
			}
		}
	}

	/**
	 * Restore the previous theme and remove the candidate theme directory.
	 */
	public function cleanup(): void {
		if ( null !== $this->previous ) {
			foreach ( $this->previous as $option => $value ) {
				update_option( $option, $value );
			}
			$this->previous = null;
			self::refresh_theme_state();
		}
		parent::cleanup();
		$this->slug = null;
		wp_clean_themes_cache();
	}

	/**
	 * Include a theme's functions.php at global scope, as wp-settings.php does.
	 *
	 * A plain include, not include_once: when the candidate is a child of the
	 * theme the site booted with, that parent's functions.php was already
	 * included once and must run again after its hooks were removed.
	 *
	 * @param string $file Absolute path of the functions.php file.
	 */
	private static function include_theme_functions( string $file ): void {
		include $file;

		foreach ( get_defined_vars() as $wpbp_name => $wpbp_value ) {
			if ( 'file' === $wpbp_name ) {
				continue;
			}
			$GLOBALS[ $wpbp_name ] = $wpbp_value;
		}
	}

	/**
	 * Forget everything WordPress cached about the active theme.
	 */
	private static function refresh_theme_state(): void {
		wp_clean_themes_cache();
		wp_set_template_globals();
		wp_clean_theme_json_cache();
		\WP_Theme_JSON_Resolver::clean_cached_data();
	}

	/**
	 * Remove every hook callback defined in a file below one of the directories.
	 *
	 * @param string[] $dirs Absolute theme directories.
	 */
	private function unhook_theme_files( array $dirs ): void {
		$prefixes = array_map(
			static fn ( string $dir ): string => trailingslashit( wp_normalize_path( $dir ) ),
			$dirs
		);

		foreach ( $GLOBALS['wp_filter'] as $hook_name => $hook ) {
			foreach ( $hook->callbacks as $priority => $callbacks ) {
				foreach ( $callbacks as $callback ) {
					$file = self::callback_file( $callback['function'] );
					if ( null === $file ) {
						continue;
					}
					foreach ( $prefixes as $prefix ) {
						if ( str_starts_with( $file, $prefix ) ) {
							remove_filter( (string) $hook_name, $callback['function'], (int) $priority );
							break;
						}
					}
				}
			}
		}
	}

	/**
	 * File that defines a hook callback, or null when it cannot be resolved.
	 *
	 * @param mixed $callback Registered callback.
	 */
	private static function callback_file( mixed $callback ): ?string {
		try {
			if ( is_array( $callback ) && 2 === count( $callback ) ) {
				$reflection = new \ReflectionMethod( $callback[0], (string) $callback[1] );
			} elseif ( is_string( $callback ) && str_contains( $callback, '::' ) ) {
				$reflection = new \ReflectionMethod( $callback );
			} elseif ( is_object( $callback ) && ! $callback instanceof \Closure ) {
				$reflection = new \ReflectionMethod( $callback, '__invoke' );
			} elseif ( is_string( $callback ) || $callback instanceof \Closure ) {
				$reflection = new \ReflectionFunction( $callback );
			} else {
				return null;
			}
		} catch ( \ReflectionException $e ) {
			return null;
		}
		$file = $reflection->getFileName();
		return false === $file ? null : wp_normalize_path( $file );
	}
}
