<?php
/** Trusted fixture snapshots shared by PHP and shell execution tests. */
declare(strict_types=1);

namespace WPBench\Runtime;

final class Preservation implements \Countable {

	private function __construct( private string $kind, private array $records ) {}

	/** Capture every existing non-revision post, including unrelated content. */
	public static function capture_posts(): self {
		global $wpdb;
		$records = array();
		foreach ( $wpdb->get_col( "SELECT ID FROM {$wpdb->posts} WHERE post_type != 'revision'" ) as $id ) {
			$records[ $id ] = self::post_state( (int) $id );
		}
		return new self( 'post', $records );
	}

	/** Capture account data and all stored user metadata, including roles. */
	public static function capture_users(): self {
		global $wpdb;
		$records = array();
		foreach ( $wpdb->get_col( "SELECT ID FROM {$wpdb->users}" ) as $id ) {
			$records[ $id ] = self::user_state( (int) $id );
		}
		return new self( 'user', $records );
	}

	/** Number of records captured, for separate creation/count assertions. */
	public function count(): int {
		return count( $this->records );
	}

	/**
	 * Compare existing records strictly, except for explicitly allowed changes.
	 *
	 * Allowlists are keyed by captured record ID. Each entry may contain:
	 * - fields: exact post/account field names that may change.
	 * - meta: exact metadata keys that may change, appear, or disappear.
	 * - meta_prefixes: literal metadata prefixes with the same permission.
	 * - terms: post taxonomies whose relationships may change (category/post_tag).
	 * - deleted: true when the record must have been deleted.
	 *
	 * This only checks preservation; task assertions must check the intended new
	 * values and any new records. No timestamps or publishing metadata are
	 * exempt by default. Setup captures the baseline in the verifier process;
	 * shell candidates cannot supply it. This is not a PHP security boundary.
	 *
	 * @param array<int, array<string, mixed>> $allow Per-record allowances.
	 */
	public function preserved( array $allow = array() ): bool {
		foreach ( $allow as $id => $changes ) {
			if ( ! isset( $this->records[ $id ] ) ||
				array_diff( array_keys( $changes ), array( 'fields', 'meta', 'meta_prefixes', 'terms', 'deleted' ) ) ) {
				throw new \InvalidArgumentException( 'Unknown preservation record or allowance.' );
			}
		}
		foreach ( $this->records as $id => $before ) {
			$changes = $allow[ $id ] ?? array();
			$after = 'post' === $this->kind ? self::post_state( (int) $id ) : self::user_state( (int) $id );
			if ( $changes['deleted'] ?? false ) {
				if ( null !== $after ) {
					return false;
				}
				continue;
			}
			if ( null === $after ) {
				return false;
			}
			foreach ( $changes['fields'] ?? array() as $field ) {
				unset( $before['data'][ $field ], $after['data'][ $field ] );
			}
			foreach ( array_unique( array_merge( array_keys( $before['meta'] ), array_keys( $after['meta'] ) ) ) as $key ) {
				$allowed = in_array( $key, $changes['meta'] ?? array(), true );
				foreach ( $changes['meta_prefixes'] ?? array() as $prefix ) {
					$allowed = $allowed || str_starts_with( $key, $prefix );
				}
				if ( $allowed ) {
					unset( $before['meta'][ $key ], $after['meta'][ $key ] );
				}
			}
			foreach ( $changes['terms'] ?? array() as $taxonomy ) {
				unset( $before['terms'][ $taxonomy ], $after['terms'][ $taxonomy ] );
			}
			if ( $before !== $after ) {
				return false;
			}
		}
		return true;
	}

	private static function post_state( int $id ): ?array {
		$post = get_post( $id, ARRAY_A );
		if ( null === $post ) {
			return null;
		}
		// WP_Post::to_array() derives these; compare the term IDs separately.
		unset( $post['post_category'], $post['tags_input'] );
		$categories = wp_get_post_categories( $id );
		$tags = wp_get_post_tags( $id, array( 'fields' => 'ids' ) );
		sort( $categories );
		sort( $tags );
		return array(
			'data' => $post,
			'meta' => get_post_meta( $id ),
			'terms' => array( 'category' => $categories, 'post_tag' => $tags ),
		);
	}

	private static function user_state( int $id ): ?array {
		$user = get_userdata( $id );
		return $user ? array( 'data' => $user->to_array(), 'meta' => get_user_meta( $id ) ) : null;
	}
}
