<?php
/** Shared verifier services and offline WordPress hooks. */

require_once __DIR__ . '/src/class-sandbox.php';
require_once __DIR__ . '/src/class-static-analysis.php';
require_once __DIR__ . '/src/class-artifact-installer.php';
require_once __DIR__ . '/src/class-verifier.php';
require_once __DIR__ . '/src/class-shell-verifier.php';
require_once __DIR__ . '/src/class-preservation.php';

// This is an offline evaluation site. Core maintenance requests are unrelated
// to candidates and otherwise make admin hooks depend on WordPress.org access.
foreach ( array(
	'admin_init' => array( '_maybe_update_core', '_maybe_update_plugins', '_maybe_update_themes' ),
	'load-plugins.php' => array( 'wp_update_plugins' ),
	'load-themes.php' => array( 'wp_update_themes' ),
	'load-update.php' => array( 'wp_update_plugins', 'wp_update_themes' ),
	'load-update-core.php' => array( 'wp_update_plugins', 'wp_update_themes' ),
	'wp_version_check' => array( 'wp_version_check' ),
	'wp_update_plugins' => array( 'wp_update_plugins' ),
	'wp_update_themes' => array( 'wp_update_themes' ),
	'wp_maybe_auto_update' => array( 'wp_maybe_auto_update' ),
) as $hook => $callbacks ) {
	foreach ( $callbacks as $callback ) {
		remove_action( $hook, $callback );
	}
}
