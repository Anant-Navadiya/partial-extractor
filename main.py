from bs4 import BeautifulSoup, Tag, NavigableString, Comment
from datasketch import MinHash, MinHashLSH
from simhash import Simhash
from zss import simple_distance, Node
from pathlib import Path
from collections import defaultdict
import re, json, os


def write_partial(dir_path: Path, filename: str, content):
    path = dir_path / filename
    with open(path, "w", encoding="utf-8") as f:
        f.write(content if isinstance(content, str) else content.prettify())
    print(f"   📄 Created partial: {path}")


def create_include_statement(filename: str, params: dict = None) -> str:
    if params is None or not any(params.values()):
        return f"@@include('./partials/{filename}')"
    params_str = json.dumps(params, indent=4)
    return f"@@include('./partials/{filename}', {params_str})"


class HtmlRefactorer:
    MIN_NODE_COUNT = 30
    LSH_THRESHOLD = 0.6
    SIMHASH_DISTANCE = 6
    NODE_COUNT_SIMILARITY = 0.85

    PRIORITY_TAGS = ["header", "nav", "footer", "aside"]
    PARAMETERIZE_TAGS = ['h1', 'h2', 'h3', 'h4', 'a', 'img', 'span']

    STATEFUL_CLASSES = re.compile(r"\b(active|current|open|show|selected|aria-current)\b", re.I)
    DROP_ATTRS = {"onclick", "onload", "style"}
    KEEP_ATTRS = {"class", "role", "aria-label", "aria-labelledby", "href", "src", "id"}

    def __init__(self, src_dir: Path, out_dir: Path):
        self.src_dir = src_dir.resolve()
        self.out_dir = out_dir.resolve()
        self.partials_dir = self.out_dir / "partials"
        self.lsh = MinHashLSH(threshold=self.LSH_THRESHOLD, num_perm=128)
        self.items = {}
        self.clusters = []
        self.all_html_files = []
        self.common_css_hrefs = set()
        self.common_js_srcs = set()
        self.representative_link_html = {}
        self.representative_script_html = {}
        self.page_titles = {}  # Corrected variable name
        self.page_description = {}
        self.page_keywords = {}
        # NEW: Dictionary to hold parsed and tagged soup objects
        self.soups = {}

    def run(self):
        print("🚀 Starting HTML refactoring process...")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.partials_dir.mkdir(exist_ok=True)

        self._extract_common_head_and_footer()
        # ❗ MODIFIED: Call the new tagging method
        self._mine_and_tag_candidates()
        self._cluster_candidates()
        self._extract_partials()
        self._replace_in_files()

        print("\nRefactoring complete!")

    def _to_zss_node(self, node):
        label = node.name
        if node.has_attr("class") and node["class"]:
            label += ":" + ",".join(node["class"][:2])
        z_node = Node(label)
        for ch in node.children:
            if isinstance(ch, Tag):
                z_node.addkid(self._to_zss_node(ch))
        return z_node

    def _canonicalize(self, tag: Tag):
        soup_copy = BeautifulSoup(str(tag), 'html.parser')
        root_tag_in_copy = soup_copy.find(tag.name, recursive=False)

        if not root_tag_in_copy:
            return None

        # Find and remove all comments first.
        for comment in root_tag_in_copy.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()

        for el in list(root_tag_in_copy.find_all(True)):
            if el.has_attr("class"):
                stateful = {"active", "show", "current", "open", "selected", "collapsing"}
                cls = sorted([c for c in el["class"] if not self.STATEFUL_CLASSES.search(c) and c not in stateful])
                if cls:
                    el["class"] = cls
                else:
                    if 'class' in el.attrs:
                        del el['class']

            attrs_to_normalize = {}
            attrs_to_delete = []
            for attr, value in el.attrs.items():
                if attr in ["href", "data-bs-target"] and isinstance(value, str) and value.startswith("#"):
                    attrs_to_normalize[attr] = "#"
                elif attr in ["id", "aria-controls", "aria-expanded", "data-bs-toggle"]:
                    attrs_to_delete.append(attr)
                elif attr in self.DROP_ATTRS or (attr.startswith("data-") and attr not in self.KEEP_ATTRS):
                    attrs_to_delete.append(attr)

            for attr in attrs_to_delete:
                if attr in el.attrs:
                    del el.attrs[attr]
            for attr, new_val in attrs_to_normalize.items():
                el.attrs[attr] = new_val

        # Process only the remaining NavigableString text nodes.
        for t in root_tag_in_copy.find_all(string=True):
            if isinstance(t, NavigableString):
                nt = re.sub(r"\s+", " ", str(t)).strip()
                if nt:
                    t.replace_with(nt)
                else:
                    t.extract()

        return root_tag_in_copy

    def _get_node_size(self, tag: Tag):
        return len(tag.find_all(True)) + len(tag.find_all(string=lambda s: s and s.strip()))

    def _get_minhash(self, tag: Tag):
        mh = MinHash(num_perm=128)
        shingles = set()
        for p in self._get_structural_paths(tag):
            if len(p) >= 3:
                for i in range(len(p) - 3 + 1):
                    shingles.add(">".join(p[i:i + 3]))
        for sh in shingles:
            mh.update(sh.encode("utf-8"))
        return mh

    def _get_structural_paths(self, tag: Tag):
        def get_paths(node, prefix=[]):
            path = prefix + [node.name]
            element_children = [c for c in node.children if isinstance(c, Tag)]
            if not element_children:
                yield path
            else:
                for child in element_children:
                    yield from get_paths(child, path)

        return get_paths(tag)

    def _get_simhash(self, tag: Tag):
        tokens = [el.name for el in tag.descendants if isinstance(el, Tag)]
        return Simhash(tokens)

    def _longest_common_suffix(self, strings):
        if not strings: return ""
        rev = [s[::-1] for s in strings]
        shortest = min(len(s) for s in rev)
        suffix = []
        for i in range(shortest):
            chars = {s[i] for s in rev}
            if len(chars) == 1:
                suffix.append(chars.pop())
            else:
                break
        return "".join(reversed(suffix))

    def _extract_common_head_and_footer(self):
        print("\nExtracting global head/footer partials...")
        html_files = sorted(self.src_dir.rglob("*.html"))
        self.all_html_files = [f.resolve() for f in html_files]

        titles = []
        meta_sets, link_sets, style_sets, head_script_sets = [], [], [], []
        rep_meta, rep_link, rep_style, rep_script = {}, {}, {}, {}

        for p in self.all_html_files:
            soup = BeautifulSoup(p.read_text(encoding="utf-8", errors="ignore"), "html5lib")
            head = soup.head
            if not head: continue

            t = head.find("title").string.strip() if head.find("title") and head.find("title").string else ""
            titles.append(t)
            metas = {str(m) for m in head.find_all("meta") if not m.get("charset")}
            rep_meta.update({str(m): str(m) for m in head.find_all("meta")})
            meta_sets.append(metas)
            links = {str(l) for l in head.find_all("link") if l.get("rel") != ["stylesheet"]}
            rep_link.update({str(l): str(l) for l in head.find_all("link")})
            link_sets.append(links)
            styles = {str(s) for s in head.find_all("style")}
            rep_style.update({str(s): str(s) for s in head.find_all("style")})
            style_sets.append(styles)
            h_scripts = {str(s) for s in head.find_all("script") if s.get("src") or s.string}
            rep_script.update({str(s): str(s) for s in head.find_all("script")})
            head_script_sets.append(h_scripts)

        common_metas = set.intersection(*meta_sets) if meta_sets else set()
        common_links = set.intersection(*link_sets) if link_sets else set()
        common_styles = set.intersection(*style_sets) if style_sets else set()
        common_head_scripts = set.intersection(*head_script_sets) if head_script_sets else set()

        common_suffix = self._longest_common_suffix(titles)
        title_line = "<title>{{ page_title }}</title>"
        if common_suffix:
            title_line = f"<title>{{{{ page_title }}}} {common_suffix}</title>"

        self.page_titles = {
            str(p): (t[:len(t) - len(common_suffix)].strip(" |-") if common_suffix else t)
            for p, t in zip(self.all_html_files, titles)
        }

        lines = [title_line] + [rep_meta[m] for m in sorted(common_metas)] + [rep_link[l] for l in sorted(common_links)]
        write_partial(self.partials_dir, "title-meta.html", "\n".join(lines))

        css_lines = []
        if self.all_html_files:
            first_soup = BeautifulSoup(self.all_html_files[0].read_text(encoding="utf-8", errors="ignore"), "html5lib")
            if first_soup.head:
                for l in first_soup.head.find_all("link", rel="stylesheet"):
                    css_lines.append(str(l))
        css_lines += [rep_style[s] for s in sorted(common_styles)]
        css_lines += [rep_script[s] for s in sorted(common_head_scripts)]
        write_partial(self.partials_dir, "head-css.html", "\n".join(css_lines))

        all_script_sets, rep_script_footer = [], {}
        for p in self.all_html_files:
            soup = BeautifulSoup(p.read_text(encoding="utf-8", errors="ignore"), "html5lib")
            body = soup.body
            if not body: continue
            srcs = set()
            for s in body.find_all("script", src=True):
                srcs.add(s.get("src"))
                rep_script_footer[s.get("src")] = str(s)
            all_script_sets.append(srcs)

        common_js = set.intersection(*all_script_sets) if all_script_sets else set()
        footer_lines = [rep_script_footer[src] for src in sorted(common_js)]
        write_partial(self.partials_dir, "footer-scripts.html", "\n".join(footer_lines))
        self.common_js_srcs = common_js

    def _mine_and_tag_candidates(self):
        print("\n[1/4] 🔍 Mining and tagging candidate components...")
        idx = 0
        for html_path in self.all_html_files:
            try:
                soup = BeautifulSoup(html_path.read_text(encoding="utf-8", errors="ignore"), "html5lib")
                body = soup.body or soup
                found_tags = set()

                for tag_name in self.PRIORITY_TAGS:
                    for el in body.find_all(tag_name):
                        if self._get_node_size(el) >= self.MIN_NODE_COUNT:
                            found_tags.add(el)

                for el in body.find_all(["div", "section"], recursive=False):
                    if self._get_node_size(el) >= self.MIN_NODE_COUNT:
                        found_tags.add(el)

                for tag in found_tags:
                    key = f"c_{idx}"
                    tag['data-refactor-id'] = key

                    # Capture the original, raw HTML of the tag
                    raw_tag_html = str(tag)

                    # Create a copy for canonicalization, leaving original tag unmodified
                    tag_copy = BeautifulSoup(raw_tag_html, "html5lib").find(tag.name)
                    if tag_copy and tag_copy.has_attr('data-refactor-id'):
                        del tag_copy['data-refactor-id']

                    canonical_tag = self._canonicalize(tag_copy)
                    if canonical_tag:
                        mh = self._get_minhash(canonical_tag)
                        simh = self._get_simhash(canonical_tag)
                        self.lsh.insert(key, mh)
                        # Store the raw_tag_html along with the canonical_tag
                        self.items[key] = (html_path, raw_tag_html, canonical_tag, mh, simh)
                        idx += 1

                self.soups[html_path] = soup

            except Exception as e:
                print(f"Could not process {html_path}: {e}")
        print(f"Found and tagged {len(self.items)} candidates.")

    def _cluster_candidates(self):
        print("\nClustering...")
        sorted_keys = sorted(self.items.keys())
        visited = set()
        for key in sorted_keys:
            if key in visited:
                continue

            # Unpack 5 items instead of 4 to account for raw_tag_html
            _, _, seed_tag, seed_mh, seed_simh = self.items[key]

            near_keys = self.lsh.query(seed_mh)
            cluster = [(key, self.items[key])]
            visited.add(key)

            for nk in near_keys:
                if nk == key or nk in visited:
                    continue

                # Unpack 5 items here as well
                _, _, near_tag, _, near_simh = self.items[nk]

                if seed_simh.distance(near_simh) > self.SIMHASH_DISTANCE:
                    continue

                n1, n2 = self._get_node_size(seed_tag), self._get_node_size(near_tag)
                if n1 > 0 and n2 > 0 and min(n1, n2) / max(n1, n2) < self.NODE_COUNT_SIMILARITY:
                    continue

                cluster.append((nk, self.items[nk]))
                visited.add(nk)

            if len(cluster) > 1:
                self.clusters.append(cluster)
        print(f"   Found {len(self.clusters)} clusters.")

    def _extract_partials(self):
        print("\nExtracting partials...")
        final_clusters_info = []

        for i, cluster in enumerate(self.clusters):
            medoid_key, medoid_data = cluster[0]
            # Unpack the new data structure including the raw html
            medoid_path, medoid_raw_html, medoid_tag, _, _ = medoid_data

            if not medoid_tag or not hasattr(medoid_tag, 'name'):
                print(f"   ⚠️  Skipping invalid cluster {i + 1} (medoid was empty).")
                continue

            # Use the pristine, raw HTML to create the partial
            partial_soup = BeautifulSoup(medoid_raw_html, 'html.parser')
            # Find the first tag, which is our component
            medoid_template = partial_soup.find()

            # Clean the temporary refactor-id from the version we're saving
            if medoid_template and medoid_template.has_attr('data-refactor-id'):
                del medoid_template['data-refactor-id']

            partial_name = f"partial_{i + 1}_{medoid_template.name}.html"
            write_partial(self.partials_dir, partial_name, medoid_template)

            cluster_instances = []
            # Adjust unpacking for the loop
            for key, (path, _, _, _, _) in cluster:
                cluster_instances.append({"refactor_id": key, "source_file": str(path)})

            final_clusters_info.append({
                "partial_file": partial_name,
                "instances": cluster_instances
            })

        self.clusters = final_clusters_info

    def _replace_in_files(self):
        print("\nReplacing HTML with import statements...")

        for p, soup in self.soups.items():
            head, body = soup.head, soup.body

            # Replace cluster instances by refactor-id
            for cluster in self.clusters:
                include_stmt = create_include_statement(cluster["partial_file"])
                for inst in cluster["instances"]:
                    # Process only instances belonging to the current file
                    if Path(inst["source_file"]).resolve() == p:
                        ref_id = inst["refactor_id"]
                        target = soup.find(attrs={"data-refactor-id": ref_id})
                        if target:
                            target.replace_with(BeautifulSoup(include_stmt, "html.parser"))

            # HEAD replacement
            if head:
                # Clear existing head tags that will be replaced by partials
                for tag_type in ["title", "meta", "link", "style", "script"]:
                    for tag in head.find_all(tag_type):
                        # A bit more careful decomposition
                        if tag_type == "link" and tag.get("rel") == ["stylesheet"]:
                            tag.decompose()
                        elif tag_type != "link":
                            tag.decompose()

                page_title = self.page_titles.get(str(p), "")
                title_meta_include = create_include_statement(
                    "title-meta.html",
                    {"page_title": page_title} if page_title else None
                )
                head.insert(0, BeautifulSoup(title_meta_include, "html.parser"))
                head.append(BeautifulSoup("@@include('./partials/head-css.html')", "html.parser"))

            # FOOTER replacement
            if body:
                for tag in body.find_all("script", src=True):
                    if tag.get("src") in self.common_js_srcs:
                        tag.decompose()
                body.append(BeautifulSoup("@@include('./partials/footer-scripts.html')", "html.parser"))

            out_path = self.out_dir / p.relative_to(self.src_dir)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(soup.prettify(), encoding="utf-8")
            print(f"   Updated {p.relative_to(self.src_dir)}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Automatically extract/refactor HTML partials")
    parser.add_argument("src", type=str, help="Source directory with HTML files.")
    parser.add_argument("dest", type=str, help="Destination directory for refactored files.")
    args = parser.parse_args()
    refactorer = HtmlRefactorer(Path(args.src), Path(args.dest))
    refactorer.run()
