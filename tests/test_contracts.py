"""Contract tests: example configs match the schema, and every GraphQL document in
bin/autopilot-linear validates against Linear's published schema.

Needs `pyyaml jsonschema graphql-core` (CI installs them); skips otherwise.
The Linear schema is downloaded once into .schema-cache/ (or set LINEAR_SCHEMA).
"""

import importlib.machinery
import importlib.util
import json
import os
import pathlib
import re
import unittest
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_URL = "https://raw.githubusercontent.com/linear/linear/master/packages/sdk/src/schema.graphql"

try:
    import jsonschema
    import yaml
except ImportError:  # pragma: no cover
    yaml = jsonschema = None

try:
    import graphql
except ImportError:  # pragma: no cover
    graphql = None


@unittest.skipIf(yaml is None, "pyyaml/jsonschema not installed")
class ConfigSchemaTests(unittest.TestCase):
    def test_schema_is_valid_and_examples_conform(self):
        schema = json.loads((ROOT / "schema" / "autopilot.schema.json").read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        examples = sorted((ROOT / "examples").glob("*/autopilot.yaml"))
        self.assertGreaterEqual(len(examples), 2)
        for path in examples:
            with self.subTest(example=path.parent.name):
                jsonschema.validate(yaml.safe_load(path.read_text()), schema)

    def test_web_app_example_keeps_no_prod_rules(self):
        cfg = yaml.safe_load((ROOT / "examples" / "web-app" / "autopilot.yaml").read_text())
        self.assertIn("example-prod", cfg["forbidden"]["gcp_projects"])
        self.assertIn("api.example.com", cfg["forbidden"]["hosts"])
        self.assertFalse(cfg["git"]["ticket_id_in_branch_or_title"])
        self.assertLessEqual(cfg["spend"]["paid_model_usd_per_ticket"], 1)

    def test_preprod_sign_in(self):
        schema = json.loads((ROOT / "schema" / "autopilot.schema.json").read_text())
        cfg = yaml.safe_load((ROOT / "examples" / "web-app" / "autopilot.yaml").read_text())
        self.assertNotIn("sign_in", cfg["preprod"])  # opt-in: the example leaves it commented out
        cfg["preprod"]["sign_in"] = {"with": "passhand", "user": "test-user", "keep_signed_in": "Keep me logged in"}
        jsonschema.validate(cfg, schema)
        cfg["preprod"]["sign_in"] = {"with": "passhand"}  # user defaults to keepalive.user
        jsonschema.validate(cfg, schema)
        for bad in ({"user": "test-user"}, {"with": "typing", "user": "test-user"},
                    {"with": "passhand", "user": "test-user", "password": "x"}):
            with self.subTest(sign_in=bad):
                cfg["preprod"]["sign_in"] = bad
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(cfg, schema)


    def test_preprod_browser_host(self):
        schema = json.loads((ROOT / "schema" / "autopilot.schema.json").read_text())
        cfg = yaml.safe_load((ROOT / "examples" / "web-app" / "autopilot.yaml").read_text())
        self.assertNotIn("browser_host", cfg["preprod"])  # opt-in: the example leaves it commented out
        cfg["preprod"]["browser_host"] = {"user": "test-user", "port": 9333, "allow_hosts": ["cdn.example.net"],
                                          "dismiss": ["Maybe Later"], "sign_in_button": "Sign In"}
        jsonschema.validate(cfg, schema)
        cfg["preprod"]["browser_host"] = {}
        jsonschema.validate(cfg, schema)
        for bad in ({"port": 80}, {"dismiss": "Maybe Later"}, {"profile": "/tmp/x"}):
            with self.subTest(browser_host=bad):
                cfg["preprod"]["browser_host"] = bad
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(cfg, schema)

def _linear_schema():
    path = os.environ.get("LINEAR_SCHEMA")
    if path:
        return pathlib.Path(path).read_text()
    cache = ROOT / ".schema-cache" / "linear.graphql"
    if not cache.exists():
        cache.parent.mkdir(exist_ok=True)
        with urllib.request.urlopen(SCHEMA_URL, timeout=30) as r:
            cache.write_bytes(r.read())
    return cache.read_text()


@unittest.skipIf(graphql is None, "graphql-core not installed")
class GraphQLContractTests(unittest.TestCase):
    def test_all_documents_validate_against_linear_schema(self):
        src_path = ROOT / "bin" / "autopilot-linear"
        loader = importlib.machinery.SourceFileLoader("apl_contract", str(src_path))
        spec = importlib.util.spec_from_loader("apl_contract", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        schema = graphql.build_schema(_linear_schema())
        # Inline mutations are written as adjacent string literals; join them back up.
        joined = re.sub(r'"\s*\n\s*"', "", src_path.read_text())
        mutations = re.findall(r'"(mutation\(.*?)",\s*\n', joined)
        docs = [mod.QUEUE_QUERY, mod.ISSUE_QUERY, mod.SESSIONS_QUERY, mod.STATES_QUERY, mod.AUTOMATIONS_QUERY, mod.CREATE_CONTEXT_QUERY, mod.USER_QUERY, mod.USERS_QUERY,
                "query { viewer { id name displayName app } }"] + mutations
        # The gateway Worker's own mutation (it posts the "Queued" thought).
        gw_src = (ROOT / "gateway" / "src" / "index.js").read_text()
        gw_mutations = re.findall(r'"(mutation\(.*?)",\s*\n', gw_src)
        self.assertEqual(len(gw_mutations), 1)
        # ... and its GitHub mention handling (looks up the issue, opens a session on it).
        gh_src = (ROOT / "gateway" / "src" / "github.js").read_text()
        gh_docs = re.findall(r'"((?:query|mutation)\(.*?)",\s*\n?\s*\{', gh_src)
        self.assertEqual(len(gh_docs), 2)
        self.assertGreaterEqual(len(mutations), 6)
        docs += gw_mutations + gh_docs
        for doc in docs:
            with self.subTest(doc=doc.strip().splitlines()[0][:60]):
                errors = graphql.validate(schema, graphql.parse(doc))
                self.assertEqual([e.message for e in errors], [])


if __name__ == "__main__":
    unittest.main()
