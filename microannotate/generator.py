# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import asyncio
import concurrent.futures
import itertools
import multiprocessing
import os
import re
import subprocess
import threading
import time
import traceback
from logging import INFO, basicConfig, getLogger

import aiofiles
import aiohttp
import hglib
import pygit2
from tqdm import tqdm

from microannotate import utils

basicConfig(level=INFO)
logger = getLogger(__name__)


thread_local = threading.local()


class Commit:
    def __init__(self, node, parents, desc):
        self.node = node
        self.parents = parents
        self.desc = desc

    def __eq__(self, other):
        assert isinstance(other, Commit)
        return self.node == other.node

    def __hash__(self):
        return hash(self.node)


def _init_process(repo_dir):
    global HG
    os.chdir(repo_dir)
    HG = hglib.open(".")


def _init_thread():
    thread_local.hg = hglib.open(".")


def set_modified_files(commit):
    template = '{join(files,"|")}'
    args = hglib.util.cmdbuilder(
        b"log", template=template, rev=commit.node.encode("ascii")
    )
    files_str = HG.rawcommand(args)

    commit.files = files_str.split(b"|")


def hg_log(hg, revs):
    template = "{node}\\0{p1node}\\0{desc}\\0"

    args = hglib.util.cmdbuilder(
        b"log", template=template, rev=revs[0] + b":" + revs[-1]
    )
    x = hg.rawcommand(args)
    out = x.split(b"\x00")[:-1]

    revs = []
    for rev in hglib.util.grouper(template.count("\\0"), out):
        revs.append(
            Commit(
                node=rev[0].decode("ascii"),
                parents=rev[1].decode("ascii").split(" "),
                desc=rev[2].decode("utf-8"),
            )
        )

    return revs


def _hg_log(revs):
    return hg_log(thread_local.hg, revs)


def hg_cat(hg, path, rev):
    return hg.cat([path], rev)


def _hg_cat(path, rev):
    return hg_cat(thread_local.hg, path, rev)


def get_revs(hg, rev_start=0, rev_end="tip"):
    logger.info(f"Getting revs from {rev_start} to {rev_end}...")

    args = hglib.util.cmdbuilder(
        b"log",
        template="{node}\n",
        no_merges=True,
        branch="central",
        rev=f"{rev_start}:{rev_end}",
    )
    x = hg.rawcommand(args)
    return x.splitlines()


SPLIT_WORD_REGEX = re.compile(rb"(\w+|{|}|\[|\]|\"|'|\(|\)|\\\\|\*|#|/)")


class Generator:
    def __init__(
        self,
        repo_dir,
        repo_out_dir,
        rev_start=0,
        rev_end="tip",
        limit=None,
        tokenize=True,
        remove_comments=False,
    ):
        self.repo_dir = repo_dir
        self.repo_out_dir = repo_out_dir
        self.rev_start = rev_start
        self.rev_end = rev_end
        self.limit = limit
        self.tokenize_enabled = tokenize
        self.remove_comments_enabled = remove_comments

    async def remove_comments(self, path, content):
        try:
            async with self.session.post(
                f"http://localhost:{self.code_analysis_port}/comment?file_name={path}",
                data=content,
            ) as r:
                # The server returns 200 when successful, 204 when no comments have been removed and
                # 404 when an extension is not supported.

                text = await r.text()

                if r.status == 200:
                    content = text.encode("utf-8")
                elif r.status not in [204, 404]:
                    logger.error(
                        f"Error {r.status} from the code analysis server, for {path}: {text}"
                    )
        except aiohttp.ClientConnectionError as e:
            logger.error(f"Error connecting to code analysis server, for {path}: {e}")

        return content

    async def write_file(self, commit, path):
        loop = asyncio.get_event_loop()

        try:
            content = await loop.run_in_executor(None, _hg_cat, path, commit.node)
        except hglib.error.CommandError as e:
            if b"no such file in rev" in e.err:
                # The file was removed.
                os.remove(os.path.join(self.repo.workdir, path.decode("ascii")))
                self.repo.index.remove(path)
                return
            else:
                raise

        path = path.decode("ascii")

        os.makedirs(
            os.path.dirname(os.path.join(self.repo.workdir, path)), exist_ok=True
        )

        if self.remove_comments_enabled:
            content = await self.remove_comments(path, content)

        async with aiofiles.open(os.path.join(self.repo.workdir, path), "wb") as f:
            if self.tokenize_enabled:
                await f.writelines(
                    word.group(0) + b"\n" for word in SPLIT_WORD_REGEX.finditer(content)
                )
            else:
                await f.write(content)

        self.repo.index.add(path)

    async def convert(self, commit):
        set_modified_files(commit)

        logger.info(f"Transforming commit {commit.node}")

        write_file_futures = [self.write_file(commit, path) for path in commit.files]

        results = await asyncio.gather(*write_file_futures, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                raise result

        # TODO: Support merges?
        if self.repo.head_is_unborn:
            parent = []
        else:
            parent = [self.repo.head.target]

        self.repo.index.write()
        tree = self.repo.index.write_tree()

        # TODO: Use hg author!
        author = pygit2.Signature("Marco Castelluccio", "mcastelluccio@mozilla.com")

        self.repo.create_commit(
            "HEAD",
            author,
            author,
            f"{commit.desc}\n\nUltraBlame original commit: {commit.node}",
            tree,
            parent,
        )

    async def go(self):
        headers = {"Content-Type": "text/plain"}
        async with aiohttp.ClientSession(headers=headers) as session:
            self.session = session

            proc = None
            self.code_analysis_port = None
            if self.remove_comments_enabled:
                ready = False

                for _ in range(7):
                    try:
                        self.code_analysis_port = utils.get_free_tcp_port()
                        proc = subprocess.Popen(
                            [
                                "rust-code-analysis",
                                "--serve",
                                "--port",
                                str(self.code_analysis_port),
                            ]
                        )
                    except FileNotFoundError:
                        raise Exception(
                            "rust-code-analysis is required for comment removal"
                        )

                    for _ in range(7):
                        try:
                            await self.session.get(
                                f"http://localhost:{self.code_analysis_port}/ping",
                                raise_for_status=True,
                            )
                            ready = True
                            break
                        except Exception:
                            if proc.poll() is not None:
                                break

                            time.sleep(1)

                    if ready:
                        break

                assert ready, "rust-code-analysis should be able to start"

            if os.path.exists(self.repo_out_dir):
                self.repo = pygit2.Repository(self.repo_out_dir)
                try:
                    last_commit_hash = utils.get_original_hash(self.repo, "HEAD")
                    self.rev_start = f"children({last_commit_hash})"
                except KeyError:
                    pass
            else:
                os.makedirs(self.repo_out_dir)
                self.repo = pygit2.init_repository(self.repo_out_dir)

            with hglib.open(self.repo_dir) as hg:
                revs = get_revs(hg, self.rev_start, self.rev_end)

                assert (
                    len(revs) > 0
                ), "There should definitely be more than 0 commits, something is wrong"

            all_commits_done = True
            if self.limit is not None:
                if len(revs) > self.limit:
                    all_commits_done = False

                revs = revs[: self.limit]

            logger.info(f"Mining {len(revs)} commits...")

            cwd = os.getcwd()
            os.chdir(self.repo_dir)

            CHUNK_SIZE = 256
            revs_groups = [
                revs[i : (i + CHUNK_SIZE)] for i in range(0, len(revs), CHUNK_SIZE)
            ]

            with concurrent.futures.ThreadPoolExecutor(
                initializer=_init_thread, max_workers=multiprocessing.cpu_count() + 1
            ) as executor:
                commits = executor.map(_hg_log, revs_groups)
                commits = tqdm(commits, total=len(revs_groups))
                commits = list(itertools.chain.from_iterable(commits))

                commits_num = len(commits)

                logger.info(f"Converting {commits_num} commits...")

                loop = asyncio.get_running_loop()
                loop.set_default_executor(executor)

                with open("errors.txt", "a", buffering=1) as f:
                    _init_process(self.repo_dir)
                    for commit in tqdm(commits):
                        try:
                            await self.convert(commit)
                        except Exception as e:
                            logger.error(f"Error during transformation: {e}")
                            traceback.print_exc()
                            f.write(f"{commit.node} - {commit.parents}\n")

            os.chdir(cwd)

            if proc is not None:
                proc.terminate()

            return all_commits_done


def generate(
    repo_dir,
    repo_out_dir,
    rev_start=0,
    rev_end="tip",
    limit=None,
    tokenize=True,
    remove_comments=False,
):
    generator = Generator(
        repo_dir, repo_out_dir, rev_start, rev_end, limit, tokenize, remove_comments
    )
    asyncio.run(generator.go())
