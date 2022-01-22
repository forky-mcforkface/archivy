from pathlib import Path
from shutil import which
from subprocess import run, PIPE
import json

from flask import current_app

from archivy.helpers import get_elastic_client


# Example command ["rg", RG_MISC_ARGS, RG_FILETYPE, RG_REGEX_ARG, query, str(get_data_dir())]
#  rg -il -t md -e query files
# -i -> case insensitive
# -l -> only output filenames
# -t -> file type
# -e -> regexp
RG_MISC_ARGS = "-it"
RG_REGEX_ARG = "-e"
RG_FILETYPE = "md"


def add_to_index(model):
    """
    Adds dataobj to given index. If object of given id already exists, it will be updated.

    Params:

    - **index** - String of the ES Index. Archivy uses `dataobj` by default.
    - **model** - Instance of `archivy.models.Dataobj`, the object you want to index.
    """
    es = get_elastic_client()
    if not es:
        return
    payload = {}
    for field in model.__searchable__:
        payload[field] = getattr(model, field)
    es.index(
        index=current_app.config["SEARCH_CONF"]["index_name"], id=model.id, body=payload
    )
    return True


def remove_from_index(dataobj_id):
    """Removes object of given id"""
    es = get_elastic_client()
    if not es:
        return
    es.delete(index=current_app.config["SEARCH_CONF"]["index_name"], id=dataobj_id)


def query_es_index(query, strict=False):
    """
    Returns search results for your given query

    Specify strict=True if you want only exact result (in case you're using ES.
    """
    es = get_elastic_client()
    if not es:
        return []
    search = es.search(
        index=current_app.config["SEARCH_CONF"]["index_name"],
        body={
            "query": {
                "multi_match": {
                    "query": query,
                    "fields": ["*"],
                    "analyzer": "rebuilt_standard",
                }
            },
            "highlight": {
                "fragment_size": 0,
                "fields": {
                    "content": {
                        "pre_tags": "",
                        "post_tags": "",
                    }
                },
            },
        },
    )

    hits = []
    for hit in search["hits"]["hits"]:
        formatted_hit = {"id": hit["_id"], "title": hit["_source"]["title"]}
        if "highlight" in hit:
            formatted_hit["matches"] = hit["highlight"]["content"]
            reformatted_match = " ".join(formatted_hit["matches"])
            if strict and not (query in reformatted_match):
                continue
        hits.append(formatted_hit)
    return hits

def parse_ripgrep_line(line):
    hit = json.loads(line)
    data = {}
    if hit["type"] == "begin":
        curr_file = (
            Path(hit["data"]["path"]["text"])
            .parts[-1]
            .replace(".md", "")
            .split("-")
        )  # parse target note data from path
        curr_id = int(curr_file[0])
        title = curr_file[-1].replace("_", " ")
        data = {"title": title, "matches": [], "id": curr_id}
    elif hit["type"] == "match":
        data = hit["data"]["lines"]["text"].strip()
        if data.startswith("tags: [") or data.startswith("title:"):
            return None
    else: return None
    return (data, hit["type"])


def query_ripgrep(query):
    """
    Uses ripgrep to search data with a simpler setup than ES.
    Returns a list of dicts with detailed matches.
    """

    from archivy.data import get_data_dir

    if not which("rg"):
        return []

    rg_cmd = ["rg", RG_MISC_ARGS, RG_FILETYPE, "--json", query, str(get_data_dir())]
    rg = run(rg_cmd, stdout=PIPE, stderr=PIPE, timeout=60)
    output = rg.stdout.decode().splitlines()
    hits = {}
    curr_id = None
    for line in output:
        parsed = parse_ripgrep_line(line)
        if not parsed: continue
        if parsed[1] == "begin":
            curr_id = parsed[0]["id"]
            hits[curr_id] = parsed[0]
        if parsed[1] == "match":
            hits[curr_id]["matches"].append(parsed[0])
    return sorted(
        list(hits.values()), key=lambda x: len(x["matches"]), reverse=True
    )  # sort by number of matches

def search_frontmatter_tags(tag=None):
    """
    Returns a list of dataobj ids that have the given tag.
    """
    from archivy.data import get_data_dir

    if not which("rg"):
        return []
    META_PATTERN = r"(^|\n)tags:(\n- [_a-zA-ZÀ-ÖØ-öø-ÿ0-9]+)+"
    hits = []
    rg_cmd = ["rg", "-Uo", RG_MISC_ARGS, RG_FILETYPE, "--json", RG_REGEX_ARG, META_PATTERN, str(get_data_dir())]
    rg = run(rg_cmd, stdout=PIPE, stderr=PIPE, timeout=60)
    output = rg.stdout.decode().splitlines()
    for line in output:
        parsed = parse_ripgrep_line(line)
        if not parsed: continue
        if parsed[1] == "begin":
            hits.append(parsed[0])
        if parsed[1] == "match":
            sanitized = parsed[0].replace("- ", "").split("\n")[2:]
            hits[-1]["tags"] = hits[-1].get("tags", []) + sanitized
    if tag:
        hits = list(filter(lambda x: tag in x["tags"], hits))
    return hits

def query_ripgrep_tags():
    """
    Uses ripgrep to search for tags.
    Mandatory reference: https://xkcd.com/1171/
    """

    EMB_PATTERN = r"(^|\n| )#([-_a-zA-ZÀ-ÖØ-öø-ÿ0-9]+)#"
    from archivy.data import get_data_dir

    if not which("rg"):
        return []

    # embedded tags
    # io: case insensitive
    rg_cmd = ["rg", "-Uio", RG_FILETYPE, RG_REGEX_ARG, EMB_PATTERN, str(get_data_dir())]
    rg = run(rg_cmd, stdout=PIPE, stderr=PIPE, timeout=60)
    hits = set()
    for line in rg.stdout.splitlines():
        tag = Path(line.decode()).parts[-1].split(":")[-1]
        tag = tag.replace("#", "").lstrip()
        hits.add(tag)
    # metadata tags
    for item in search_frontmatter_tags():
        for tag in item["tags"]:
            hits.add(tag)
    return hits


def search(query, strict=False):
    """
    Wrapper to search methods for different engines.

    If using ES, specify strict=True if you only want results that strictly match the query, without parsing / tokenization.
    """
    if current_app.config["SEARCH_CONF"]["engine"] == "elasticsearch":
        return query_es_index(query, strict=strict)
    elif current_app.config["SEARCH_CONF"]["engine"] == "ripgrep" or which("rg"):
        return query_ripgrep(query)
