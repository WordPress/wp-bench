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
	require_once __DIR__ . '/src/class-cli-ability.php';
}

if ( defined( 'WP_CLI' ) && WP_CLI ) {
	\WP_CLI::add_command( 'bench verify', \WPBench\Runtime\CLI_Verifier::class );
	\WP_CLI::add_command( 'bench ability', \WPBench\Runtime\CLI_Ability::class );
}

// Register a minimal ability for tooling tests when Abilities API is available.
add_action(
	'wp_abilities_api_categories_init',
	static function (): void {
		if ( ! function_exists( 'wp_register_ability_category' ) ) {
			return;
		}
		wp_register_ability_category(
			'wpbench',
			array(
				'label'       => 'WP-Bench',
				'description' => 'Abilities registered for WP-Bench runtime tests.',
			)
		);
	}
);

add_action(
	'wp_abilities_api_init',
	static function (): void {
		if ( ! function_exists( 'wp_register_ability' ) ) {
			return;
		}

		wp_register_ability(
			'wpbench/get_site_info',
			array(
				'label'               => 'Get site info',
				'description'         => 'Returns basic site information.',
				'category'            => 'wpbench',
				'input_schema'        => array(
					'type'                 => 'object',
					'properties'           => array(),
					'additionalProperties' => false,
				),
				'output_schema'       => array(
					'type'       => 'object',
					'properties' => array(
						'name' => array( 'type' => 'string' ),
						'url'  => array( 'type' => 'string' ),
					),
					'required'   => array( 'name', 'url' ),
				),
				'execute_callback'    => static function (): array {
					return array(
						'name' => get_bloginfo( 'name' ),
						'url'  => home_url(),
					);
				},
				'permission_callback' => '__return_true',
				'meta'                => array(
					'show_in_rest' => true,
					'annotations'  => array(
						'readonly' => true,
					),
				),
			)
		);
	}
);
