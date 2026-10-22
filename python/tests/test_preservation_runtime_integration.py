"""Preservation contracts in real WordPress/SQLite; enabled by WP_BENCH_TEST_IMAGE."""
from __future__ import annotations

import os

import pytest

from wp_bench.artifacts import Artifact
from wp_bench.config import GraderConfig
from wp_bench.environment import WordPressEnvironment

IMAGE = os.environ.get("WP_BENCH_TEST_IMAGE")
pytestmark = pytest.mark.skipif(not IMAGE, reason="Set WP_BENCH_TEST_IMAGE for Docker integration tests")


@pytest.fixture(scope="module")
def environment():
    env = WordPressEnvironment(GraderConfig(image=IMAGE or "", setup_timeout_seconds=60))
    try:
        env.setup()
        yield env
    finally:
        env.close()


SETUP = r"""
require_once ABSPATH . 'wp-admin/includes/user.php';
$p = wp_insert_post(array('post_title'=>'Before', 'post_name'=>'preserve-before', 'post_status'=>'draft',
    'post_date'=>'2020-01-01 12:00:00', 'post_date_gmt'=>'2020-01-01 12:00:00'));
$q = wp_insert_post(array('post_title'=>'Unrelated', 'post_status'=>'draft'));
$u = wp_insert_user(array('user_login'=>'preserve-a', 'user_pass'=>'password', 'role'=>'subscriber'));
$v = wp_insert_user(array('user_login'=>'preserve-b', 'user_pass'=>'password', 'role'=>'subscriber'));
foreach (array($p,$q) as $id) {
    update_post_meta($id,'structured',array('count'=>0,'enabled'=>false,'null'=>null));
    update_post_meta($id,'_legacy_one','old');
    update_post_meta($id,'_legacyish','keep');
    add_post_meta($id,'duplicate','same'); add_post_meta($id,'duplicate','same');
}
foreach (array($u,$v) as $id) {
    update_user_meta($id,'profile',array('count'=>0,'enabled'=>false,'null'=>null));
}
$c = wp_insert_term('Replacement category','category')['term_id'];
$t = wp_insert_term('Replacement tag','post_tag')['term_id'];
$GLOBALS['preserve_ids'] = compact('p','q','u','v','c','t');
$GLOBALS['preserve_posts'] = WPBench\Runtime\Preservation::capture_posts();
$GLOBALS['preserve_users'] = WPBench\Runtime\Preservation::capture_users();
"""

# Each case mutates real WP state, then compares the trusted setup snapshot.
# Allowances on p/u must never permit a matching mutation on unrelated q/v.
@pytest.mark.parametrize("mutation,allow,posts,users", [
    ("", "", True, True),
    ("wp_update_post(array('ID'=>$q,'post_title'=>'Changed'));", "", False, True),
    ("wp_delete_post($q,true);", "", False, True),
    ("wp_delete_user($v);", "", True, False),
    ("delete_post_meta($p,'structured');", "", False, True),
    ("update_post_meta($p,'structured',array('count'=>'0','enabled'=>false,'null'=>null));", "", False, True),
    ("delete_post_meta($p,'duplicate'); add_post_meta($p,'duplicate','same');", "", False, True),
    ("add_post_meta($p,'new_key','unexpected');", "", False, True),
    ("update_user_meta($u,'profile',array('count'=>0,'enabled'=>'','null'=>null));", "", True, False),
    ("delete_user_meta($u,'profile');", "", True, False),
    ("wp_update_user(array('ID'=>$u,'display_name'=>'Changed'));", "", True, False),
    ("wp_update_user(array('ID'=>$u,'display_name'=>'Changed'));",
     "$ua[$u]=array('fields'=>array('display_name'));", True, True),
    ("get_userdata($u)->set_role('author');", "", True, False),
    ("get_userdata($u)->set_role('author');",
     "$ua[$u]=array('meta'=>array('wp_capabilities','wp_user_level'));", True, True),
    ("get_userdata($v)->set_role('author');",
     "$ua[$u]=array('meta'=>array('wp_capabilities','wp_user_level'));", True, False),
    ("update_user_meta($u,'profile',array('allowed'=>true));",
     "$ua[$u]=array('meta'=>array('profile'));", True, True),
    ("wp_delete_post($p,true);", "$pa[$p]=array('deleted'=>true);", True, True),
    ("wp_delete_post($q,true);", "$pa[$p]=array('deleted'=>true);", False, True),
    ("wp_delete_user($u,$v);", "$ua[$u]=array('deleted'=>true);", True, True),
    ("", "$ua[$u]=array('deleted'=>true);", True, False),
    ("delete_post_meta($p,'_legacy_one'); add_post_meta($p,'_legacy_new','allowed');",
     "$pa[$p]=array('meta_prefixes'=>array('_legacy_'));", True, True),
    ("delete_post_meta($q,'_legacy_one');",
     "$pa[$p]=array('meta_prefixes'=>array('_legacy_'));", False, True),
    ("delete_post_meta($p,'_legacyish');",
     "$pa[$p]=array('meta_prefixes'=>array('_legacy_'));", False, True),
    ("update_post_meta($q,'structured','changed');",
     "$pa[$p]=array('meta'=>array('structured'));", False, True),
    ("wp_delete_post($p,true);", "$pa[$p]=array('meta'=>array('structured'));", False, True),
    ("wp_set_post_categories($p,array($c));", "", False, True),
    ("wp_set_post_categories($p,array($c));",
     "$pa[$p]=array('terms'=>array('category'));", True, True),
    ("wp_set_post_tags($p,array($t));",
     "$pa[$p]=array('terms'=>array('category'));", False, True),
    ("wp_set_post_tags($p,array($t));",
     "$pa[$p]=array('terms'=>array('post_tag'));", True, True),
    ("wp_set_post_categories($q,array($c));",
     "$pa[$p]=array('terms'=>array('category'));", False, True),
    ("wp_update_post(array('ID'=>$p,'post_title'=>'Changed'));",
     "$pa[$p]=array('fields'=>array('post_title','post_modified','post_modified_gmt'));", True, True),
    ("wp_update_post(array('ID'=>$q,'post_title'=>'Changed'));",
     "$pa[$p]=array('fields'=>array('post_title','post_modified','post_modified_gmt'));", False, True),
    # Existing snapshots deliberately do not constrain newly created records.
    ("wp_insert_post(array('post_title'=>'New')); wp_create_user('new-user','password');", "", True, True),
])
def test_preservation_scopes_and_strict_values(environment, mutation, allow, posts, users) -> None:
    environment.reset()
    assertion = r"""
        extract($GLOBALS['preserve_ids']);
        $pa=array(); $ua=array();
    """ + allow + f"""
        $post_snapshot=$GLOBALS['preserve_posts']; $user_snapshot=$GLOBALS['preserve_users'];
        return $post_snapshot->preserved($pa) === {'true' if posts else 'false'}
            && $user_snapshot->preserved($ua) === {'true' if users else 'false'}
            && $post_snapshot->preserved($pa) === {'true' if posts else 'false'};
    """
    result = environment.execute_code("extract($GLOBALS['preserve_ids']); " + mutation, {
        "runtime_checks": {"setup": SETUP, "assertions": [{"type": "custom_assertion", "code": assertion}]},
    })
    assert result.success, result.raw


def test_publishing_side_effects_need_explicit_per_record_allowances(environment) -> None:
    environment.reset()
    result = environment.execute_code("""
        extract($GLOBALS['preserve_ids']);
        wp_update_post(array('ID'=>$p,'post_status'=>'publish'));
    """, {"runtime_checks": {"setup": SETUP, "assertions": [{"type": "custom_assertion", "code": """
        extract($GLOBALS['preserve_ids']); $before=$GLOBALS['preserve_posts'];
        $fields=array('post_status','post_modified','post_modified_gmt');
        return metadata_exists('post',$p,'_pingme') && metadata_exists('post',$p,'_encloseme')
            && !$before->preserved(array($p=>array('fields'=>$fields)))
            && $before->preserved(array($p=>array('fields'=>$fields,'meta'=>array('_pingme','_encloseme'))));
    """}]}})
    assert result.success, result.raw


def test_php_user_reassignment_preserves_content_without_timestamp_exemptions(environment) -> None:
    environment.reset()
    setup = SETUP + r"""
        extract($GLOBALS['preserve_ids']);
        wp_update_post(array('ID'=>$p,'post_author'=>$u));
        $GLOBALS['preserve_posts']=WPBench\Runtime\Preservation::capture_posts();
    """
    result = environment.execute_code("""
        extract($GLOBALS['preserve_ids']); wp_delete_user($u,$v);
    """, {"runtime_checks": {"setup": setup, "assertions": [{"type": "custom_assertion", "code": """
        extract($GLOBALS['preserve_ids']);
        return (int)get_post($p)->post_author===$v
            && $GLOBALS['preserve_posts']->preserved(array($p=>array('fields'=>array('post_author'))))
            && $GLOBALS['preserve_users']->preserved(array($u=>array('deleted'=>true)));
    """}]}})
    assert result.success, result.raw


def test_draft_date_refresh_is_allowed_only_when_declared(environment) -> None:
    environment.reset()
    setup = SETUP + r"""
        extract($GLOBALS['preserve_ids']); global $wpdb;
        $wpdb->update($wpdb->posts,array('post_date_gmt'=>'0000-00-00 00:00:00'),array('ID'=>$p));
        clean_post_cache($p);
        $GLOBALS['preserve_posts']=WPBench\Runtime\Preservation::capture_posts();
    """
    result = environment.execute_code("""
        extract($GLOBALS['preserve_ids']); wp_update_post(array('ID'=>$p,'post_author'=>$u));
    """, {"runtime_checks": {"setup": setup, "assertions": [{"type": "custom_assertion", "code": """
        extract($GLOBALS['preserve_ids']); $before=$GLOBALS['preserve_posts'];
        $fields=array('post_author','post_modified','post_modified_gmt');
        return get_post($p)->post_date !== '2020-01-01 12:00:00'
            && !$before->preserved(array($p=>array('fields'=>$fields)))
            && $before->preserved(array($p=>array('fields'=>array_merge($fields,array('post_date','post_date_gmt')))));
    """}]}})
    assert result.success, result.raw


def test_shell_snapshot_survives_subprocess_cache_and_rejects_unrelated_changes(environment) -> None:
    environment.reset()
    result = environment.execute_artifact(Artifact(kind="wp_cli_shell", code="""
        wp post meta update "$(wp post list --post_status=draft --title=Before --format=ids)" structured changed
        wp user meta update preserve-b profile changed
    """), {"runtime_checks": {"setup": SETUP, "assertions": [{"type": "custom_assertion", "code": """
        extract($GLOBALS['preserve_ids']);
        return !$GLOBALS['preserve_posts']->preserved()
            && $GLOBALS['preserve_posts']->preserved(array($p=>array('meta'=>array('structured'))))
            && !$GLOBALS['preserve_posts']->preserved()
            && !$GLOBALS['preserve_users']->preserved(array($u=>array('meta'=>array('profile'))))
            && count($GLOBALS['preserve_users']) === count(get_users());
    """}]}})
    assert result.success, result.raw


def test_allowlist_typos_fail_instead_of_silently_ignoring_checks(environment) -> None:
    environment.reset()
    result = environment.execute_code("", {"runtime_checks": {"setup": SETUP, "assertions": [{
        "type": "custom_assertion", "code": """
            extract($GLOBALS['preserve_ids']); $before=$GLOBALS['preserve_posts']; $caught=0;
            foreach (array(array(999999=>array()),array($p=>array('metas'=>array('structured'))),
                array($p=>array('meta_prefixes'=>array('')))) as $allow) {
                try { $before->preserved($allow); } catch (InvalidArgumentException $e) { ++$caught; }
            }
            return $caught===3;
        """,
    }]}})
    assert result.success, result.raw
