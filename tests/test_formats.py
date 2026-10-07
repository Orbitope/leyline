"""Code joined by a string's shape, not a call (the `format` channel), and requests made through a wrapper in
Python and C# (an `http` link only when exactly one route serves the path)."""

import io
import json
import subprocess
from contextlib import redirect_stdout

from leyline import cli, store
from leyline.formats import regex_shape, shape_of
from leyline.indexer import index

KEYS_TS = """export const nodeKey = (d: string, n: string) =>
  `dialogue/${d}/nodes/${n}`;
export const nodeTextKey = (d: string, n: string) =>
  `${nodeKey(d, n)}/text`;
export const choiceKey = (d: string, n: string, c: string) =>
  `${nodeKey(d, n)}/choices/${c}`;

export function parseKey(key: string) {
  const parts = key.split("/");
  const token = parts[0];
  if (token !== "dialogue") return null;
  return parseTail(parts.slice(2));
}

function parseTail(rest: string[]) {
  if (rest[0] !== "nodes") return null;
  if (rest.length === 3 && rest[2] === "text") return { node: rest[1] };
  if (rest[2] === "choices") return { node: rest[1], choice: rest[3] };
  return null;
}
"""

# Issue paths share the tail `nodes/<n>/choices/<c>` with the keys, but start at `nodes`: the reader of issue paths
# is not a reader of keys, and the key parser is not a reader of issue paths.
ISSUES_TS = """export function issuesOf(d: { nodes: { id: string; choices: { id: string }[] }[] }) {
  const out: string[] = [];
  for (const n of d.nodes) for (const c of n.choices) out.push(`nodes/${n.id}/choices/${c.id}`);
  return out;
}

export function pathTarget(path: string) {
  const segs = segmentsOf(path);
  if (segs[0] === "nodes" && segs[2] === "choices") return { node: segs[1], choice: segs[3] };
  return null;
}

function segmentsOf(p: string) { return p.split("/"); }
"""

GIT_TS = """export const branchRef = (b: string) => `refs/heads/${b}`;
export function isLocal(ref: string) { return ref.startsWith("refs/heads/"); }
"""

LOOKS_TS = """export const route = (id: string) => `/api/items/${id}/parts`;
export const file = (dir: string) => `data/${dir}/out.json`;
export const words = (who: string) => `hello ${who}, how are you`;
export const lore = (stem: string) => /^lore\\/([^/]+)\\/p\\/(\\d+)$/.test(stem);
"""

KEYS_TEST_TS = """import { parseKey } from "./keys";
it("parses a node key", () => {
  expect(parseKey(`dialogue/${"d1"}/nodes/${"n1"}/text`)).toEqual({ node: "n1" });
});
"""

KEYS_PY = '''import re

PREFIX = "cache"
SESSION = re.compile(r"^session/([^/]+)/items/(\\d+)$")


def profile_key(uid):
    return f"user:{uid}:profile"


def session_key(sid, n):
    return "session/%s/items/%d" % (sid, n)


def blob_key(name):
    return f"{PREFIX}/{name}/blob"


def owner_of(key):
    parts = key.split(":")
    if parts[0] == "user" and parts[2] == "profile":
        return parts[1]
    return None


def item_of(key):
    m = SESSION.match(key)
    return m.group(2) if m else None


def is_blob(key):
    kind, name, tail = key.split("/")
    return kind == "cache" and tail == "blob"


def unrelated(argv):
    return argv[1] == "user" and argv[3] == "profile"
'''

KEYS_CS = """using System.Text.RegularExpressions;

namespace Shop
{
    public static class Keys
    {
        public static string Basket(string user, int n) => $"basket/{user}/lines/{n}";

        public static string Order(string id) => string.Format("order:{0}:status", id);
    }

    public static class KeyReader
    {
        private static readonly Regex BasketKey = new Regex(@"^basket/([^/]+)/lines/(\\d+)$");

        public static bool IsBasket(string key) => BasketKey.IsMatch(key);

        public static string StatusOf(string key)
        {
            var parts = key.Split(':');
            if (parts[0] == "order" && parts[2] == "status") return parts[1];
            return null;
        }
    }
}
"""


def short(i):
    return i.split(":", 2)[-1].split("::")[-1].split("(")[0]


def links(c, channel):
    return {(short(r[0]), short(r[1])): (r[2], json.loads(r[3])) for r in c.execute(
        "SELECT src_id, dst_id, precision, attrs FROM edges WHERE kind = 'communicates'")
        if json.loads(r[3])["channel"] == channel}


def write(root, files):
    for f, text in files.items():
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(text)


def test_shapes_of_keys_not_routes_files_or_prose():
    c, h, call = ("c", "dialogue/"), ("h", None), ("call", "nodeKey")
    assert shape_of([c, h, ("c", "/nodes/"), h]) == ("/", ["dialogue", "*", "nodes", "*"])
    assert shape_of([call, ("c", "/text")]) == ("/", ["@nodeKey", "text"])     # filled in from nodeKey's own shape
    assert shape_of([("c", "user:"), h, ("c", ":profile")]) == (":", ["user", "*", "profile"])
    assert shape_of([("c", "/api/items/"), h]) is None                         # a route: the http channel's
    assert shape_of([("c", "data/"), h, ("c", "/out.json")]) is None           # a file: the file channel's
    assert shape_of([("c", "hello "), h, ("c", ", a/b/c")]) is None             # prose
    assert regex_shape(r"^dialogue\/([^/]+)\/nodes\/([^/]+)\/text$") == ("/", ["dialogue", "*", "nodes", "*", "text"], True)
    assert regex_shape(r"^lore\/([^/]+)\.md$") is None
    assert regex_shape(r"^(a|b)/c/d") is not None and regex_shape(r"a/b|c/d") is None


def test_a_key_builder_is_linked_to_the_code_that_takes_its_keys_apart(tmp_path):
    root = tmp_path / "ts"
    write(root, {"keys.ts": KEYS_TS, "issues.ts": ISSUES_TS, "git.ts": GIT_TS, "looks.ts": LOOKS_TS,
                 "keys.test.ts": KEYS_TEST_TS})
    db = tmp_path / "s.db"
    index(root, db, "ts")
    c = store.connect(db)
    got = links(c, "format")
    # A builder whose key starts with another builder's (`${nodeKey(d, n)}/text`) takes that builder's shape.
    assert got[("keys.nodeTextKey", "keys.parseTail")][1]["address"] == "dialogue/*/nodes/*/text"
    assert got[("keys.choiceKey", "keys.parseTail")][1]["address"] == "dialogue/*/nodes/*/choices/*"
    assert got[("issues.issuesOf", "issues.pathTarget")][1]["address"] == "nodes/*/choices/*"
    # The two formats share a tail, and each reader reads only its own: the one its own file writes, else the one
    # written where most writers agree.
    assert ("keys.choiceKey", "issues.pathTarget") not in got and ("issues.issuesOf", "keys.parseTail") not in got
    # nodeKey alone matches one fixed part of parseTail; git's refs, routes, files, prose and a test's sample key
    # are not formats of this program.
    assert set(got) == {("keys.nodeTextKey", "keys.parseTail"), ("keys.choiceKey", "keys.parseTail"),
                        ("issues.issuesOf", "issues.pathTarget")}
    stats = json.loads(c.execute("SELECT stats FROM extractor_coverage WHERE extractor = 'communicates:format'").fetchone()[0])
    assert stats["links"] == 3 and stats["written_in_tests"] == 1
    # Data, not control: a flow does not run from the builder into the parser.
    assert "format" not in {r[0] for r in c.execute("SELECT DISTINCT via FROM flow_steps")}
    c.close()


def test_formats_in_python_and_csharp(tmp_path):
    root = tmp_path / "mixed"
    write(root, {"py/keys.py": KEYS_PY, "cs/Keys.cs": KEYS_CS})
    db = tmp_path / "s.db"
    index(root, db, "mixed")
    c = store.connect(db)
    got = links(c, "format")
    assert got[("py.keys.profile_key", "py.keys.owner_of")][1]["address"] == "user:*:profile"   # split and compare
    assert got[("py.keys.session_key", "py.keys.item_of")][1]["read_by"] == "regex"   # the pattern, where it is used
    assert got[("py.keys.blob_key", "py.keys.is_blob")][1]["address"] == "cache/*/blob"   # a constant fills a hole
    assert got[("Shop.Keys.Basket", "Shop.KeyReader.IsBasket")][1]["address"] == "basket/*/lines/*"
    assert got[("Shop.Keys.Order", "Shop.KeyReader.StatusOf")][1]["address"] == "order:*:status"
    assert not any(dst.endswith("unrelated") for _, dst in got)   # the same words, at other places
    c.close()


def git(root, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


def test_a_pull_request_that_changes_a_key_lists_its_parsers(tmp_path, monkeypatch):
    """The review that found it: a key gained a language (`.../text` became `.../text/<lang>`) in its builder,
    while the parser of those keys, which nothing calls across, still expects the old shape."""
    root = tmp_path / "repo"
    write(root, {"src/keys.ts": KEYS_TS, "src/use.ts": 'import { nodeTextKey } from "./keys";\n'
                                                         'export const k = () => nodeTextKey("d", "n");\n'})
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "checkout", "-q", "-b", "lang")
    keys = root / "src/keys.ts"
    keys.write_text(keys.read_text().replace("`${nodeKey(d, n)}/text`", "`${nodeKey(d, n)}/text/${lang}`")
                    .replace("nodeTextKey = (d: string, n: string)", "nodeTextKey = (d: string, n: string, lang = \"en\")"))
    git(root, "commit", "-qam", "Key node text by language")
    monkeypatch.chdir(root)
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(["pr", "main"])
    page = out.getvalue()
    assert code == 0, page
    assert "Crosses the key format dialogue/*/nodes/*/text/*" in page
    assert ("**Must agree with the change:** `keys.ts.parseTail`: takes apart keys of the form dialogue/*/nodes/*/text/*,"
            " which the edit to keys.ts.nodeTextKey changed.") in page
    assert "keys.ts.parseTail` (its call must change" not in page   # it calls nothing that changed


ROUTES_PY = """from flask import Flask

app = Flask(__name__)


@app.route("/api/users/<int:uid>")
def get_user(uid):
    return {"id": uid}


@app.post("/api/users")
def add_user():
    return {"ok": True}
"""

CLIENT_PY = """import requests

BASE = "http://localhost:5000"
session = requests.Session()


def api_get(path):
    return session.get(BASE + path)


def load_user(uid):
    return api_get(f"/api/users/{uid}")


def create_user():
    return requests.request("POST", "/api/users")


def remote(uid):
    return requests.get(f"{BASE}/api/users/{uid}")


def nothing():
    return api_get("/api/none/here")


def log_path():
    return note("/var/log/app")


def note(p):
    return p
"""

TESTS_PY = """import httpx
from app import app


def test_add_user():
    client = app.test_client()
    client.open("/api/users", method="POST")


async def test_async_user():
    async with httpx.AsyncClient(app=app, base_url="http://t") as ac:
        await ac.get("/api/users/2")
"""

CONTROLLER_CS = """using Microsoft.AspNetCore.Mvc;

namespace Shop.Web
{
    [ApiController]
    [Route("api/[controller]")]
    public class ItemsController : ControllerBase
    {
        [HttpGet("{id}")]
        public IActionResult Get(int id) { return Ok(id); }

        [HttpPost]
        public IActionResult Create() { return Ok(); }
    }

    public static class Health
    {
        public static void Map(WebApplication app)
        {
            var health = app.MapGroup("health");
            health.MapGet("/live", () => "ok");
            return TypedResults.Created($"/api/items/{9}");
        }
    }
}
"""

CLIENT_CS = """using System.Net.Http;
using RestSharp;

namespace Shop.Client
{
    public class ItemsClient
    {
        private readonly HttpClient _http;
        private readonly string _base = "http://x";

        public async Task<string> Fetch(int id)
        {
            var r = await _http.GetAsync($"api/items/{id}");
            return await r.Content.ReadAsStringAsync();
        }

        public async Task<T> Get<T>(string path) { return default; }

        public async Task<object> Seven() { return await Get<object>("/api/items/7"); }

        public RestRequest Make() { return new RestRequest("api/items", Method.Post); }

        public HttpRequestMessage Ping() { return new HttpRequestMessage(HttpMethod.Get, $"{_base}/health/live"); }

        public void Log() { Console.WriteLine("/var/log/items"); }
    }
}
"""

TESTS_CS = """using Xunit;

namespace Shop.Tests
{
    public class ItemsTests
    {
        private readonly HttpClient _client;

        [Fact]
        public async Task GetsAnItem()
        {
            var r = await _client.GetAsync("/api/items/3");
        }
    }
}
"""


def test_requests_through_wrappers_in_python_and_csharp(tmp_path):
    """A request made through the program's own helper, a session's request(), a test client's open(), a base
    address in a variable, HttpClient with a relative path, RestSharp's RestRequest or an HttpRequestMessage reaches
    its route when exactly one route serves the path. An ASP.NET controller's [Route] prefix, and a minimal API's
    MapGroup prefix, are part of their routes; a Created(...) location is not a request."""
    root = tmp_path / "web"
    write(root, {"pyweb/app.py": ROUTES_PY, "pyweb/client.py": CLIENT_PY, "pyweb/tests/test_app.py": TESTS_PY,
                 "csweb/ItemsController.cs": CONTROLLER_CS, "csweb/ItemsClient.cs": CLIENT_CS,
                 "csweb/Tests/ItemsTests.cs": TESTS_CS})
    db = tmp_path / "s.db"
    index(root, db, "web")
    c = store.connect(db)
    got = {k: v[1]["address"] for k, v in links(c, "http").items()}
    c.close()
    assert got[("pyweb.client.load_user", "pyweb.app.get_user")] == "ANY /api/users/<int:uid>"
    assert got[("pyweb.client.create_user", "pyweb.app.add_user")] == "POST /api/users"
    assert got[("pyweb.client.remote", "pyweb.app.get_user")] == "GET /api/users/<int:uid>"
    assert got[("pyweb.tests.test_app.test_add_user", "pyweb.app.add_user")] == "POST /api/users"
    assert got[("pyweb.tests.test_app.test_async_user", "pyweb.app.get_user")] == "GET /api/users/<int:uid>"
    assert got[("Shop.Client.ItemsClient.Fetch", "Shop.Web.ItemsController.Get")] == "GET /api/Items/{id}"
    assert got[("Shop.Client.ItemsClient.Seven", "Shop.Web.ItemsController.Get")] == "GET /api/Items/{id}"
    assert got[("Shop.Client.ItemsClient.Make", "Shop.Web.ItemsController.Create")] == "POST /api/Items"
    assert got[("Shop.Client.ItemsClient.Ping", "Shop.Web.Health.Map")] == "GET /health/live"
    assert got[("Shop.Tests.ItemsTests.GetsAnItem", "Shop.Web.ItemsController.Get")] == "GET /api/Items/{id}"
    # Not a path any route serves, a log line, and a route's own decorator at the top of a module.
    srcs = {s for s, _ in got}
    assert not srcs & {"pyweb.client.nothing", "pyweb.client.log_path", "pyweb.app.<module>", "Shop.Client.ItemsClient.Log",
                       "Shop.Web.Health.Map"}
    assert len(got) == 10
