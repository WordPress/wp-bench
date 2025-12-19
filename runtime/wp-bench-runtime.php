<?php
/**
 * Plugin Name: WP-Bench Runtime
 * Description: Minimal WordPress runtime for executing WP-Bench verification commands.
 * Version: 0.1.0
 * Requires at least: 6.9
 * Requires PHP: 8.1
 * Author: WordPress Community
 * License: GPL-2.0-or-later
 */

declare(strict_types=1);

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

if ( file_exists( __DIR__ . '/vendor/autoload.php' ) ) {
	require_once __DIR__ . '/vendor/autoload.php';
} else {
	require_once __DIR__ . '/src/class-sandbox.php';
	require_once __DIR__ . '/src/class-static-analysis.php';
	require_once __DIR__ . '/src/class-cli-verifier.php';
}

if ( defined( 'WP_CLI' ) && WP_CLI ) {
	\WP_CLI::add_command( 'bench verify', \WPBench\Runtime\CLI_Verifier::class );
}
