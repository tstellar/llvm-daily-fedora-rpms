import argparse
import re
import copr.v3
import git
import dnf
import dnf.cli
import sys
import os
import subprocess

class CoprProject:
    UNTESTED = 0
    GOOD = 1
    BAD = 2

    def __init__(self, name):
        self.name = name
        self.index = -1
        self._commit = None
        self._status = CoprProject.UNTESTED

    def __lt__(self, other):
        return self.name < other.name


def get_snapshot_projects(chroot: str = None) -> list[str]:
    copr_client = copr.v3.Client.create_from_config_file()
    projects = []
    for p in copr_client.project_proxy.get_list(ownername='@fedora-llvm-team'):
        if not re.match(r"llvm-snapshots-big-merge-[0-9]+", p.name):
            continue
        if chroot and chroot not in list(p.chroot_repos.keys()):
            continue
        projects.append(CoprProject(p.name))
    projects.sort()
    for idx, p in enumerate(projects):
        p.index = idx 
    return projects


def get_clang_commit_for_snapshot_project(project_name: str, chroot: str) -> str:
    copr_client = copr.v3.Client.create_from_config_file()

    builds = copr_client.build_proxy.get_list('@fedora-llvm-team', project_name, packagename="llvm", status="succeeded")
    regex = re.compile("llvm-[0-9.]+~pre[0-9]+.g([0-9a-f]+)")
    for  b in builds:
        if chroot in b["chroots"]:
            print(b)
            m = regex.search(b["source_package"]["url"])
            if m:
                return m.group(1)
    return None


def test_with_copr_builds(copr_project: str, test_command: str):
    rpms = {
        "llvm",
        "clang"
    }

    print(f"Testing {copr_project}\n")
    copr_fullname = f"@fedora-llvm-team/{copr_project}"
    # Remove existing versions of clang and llvm
    with dnf.Base() as base:
        base.read_all_repos()
        base.fill_sack()
        for r in rpms:
            try: 
                base.remove(r)
            except dnf.exceptions.PackagesNotInstalledError:
                pass
        base.resolve(allow_erasing=True)
        base.do_transaction()

    # Enable the copr repo that we want to test.
    # FIXME: There is probably some way to do this via the python API, but I
    # can't figure it out.
    os.system(f"dnf copr enable -y {copr_fullname}")
    # Install clang and llvm builds to test
    with dnf.Base() as base:
        base.read_all_repos()
        base.fill_sack()
        for r in rpms:
            base.install(r)
        base.resolve(allow_erasing=True)
        base.download_packages(base.transaction.install_set)
        base.do_transaction()

    # Disable project so future installs don't use it.
    # FIXME: There is probably some way to do this via the python API, but I
    # can't figure it out.
    os.system(f"dnf copr disable -y {copr_fullname}")

    print(test_command)
    #test_command = "git -C /root/llvm-project merge-base --is-ancestor HEAD 6cac792bf9eacb1ed0c80fc7c767fc99c50e252"
    print(test_command)
    os.system(test_command)
    return False

def git_bisect(repo: git.Repo, good_commit: str, bad_commit: str, test_command: str):
    print("Running git bisect with {good_commit} and {bad_commit}")
    print(test_command)
    repo.git.bisect("start", bad_commit, good_commit)
    repo.git.bisect("run", test_command.split())
    print(repo.git.bisect("log"))
    repo.git.bisect("reset")
    return True


def main():

    parser = argparse.ArgumentParser()
    parser.add_argument('--good-commit')
    parser.add_argument('--bad-commit')
    parser.add_argument('--llvm-project-dir')
    parser.add_argument('--test-command')
    args = parser.parse_args()

    repo = git.Repo("/root/llvm-project/")

    chroot = "fedora-rawhide-x86_64"
    projects = get_snapshot_projects()
    oldest_project = None
    newest_project = None
    good_project = None
    bad_project = None
    for p in projects:
        p.commit = get_clang_commit_for_snapshot_project(p.name, chroot)
        try: 
            print("git merge-base --is-ancestor ", args.good_commit, p.commit)
            repo.git.merge_base('--is-ancestor', args.good_commit, p.commit)
            oldest_project = p
            break
        except:
            continue
    print(oldest_project.commit, oldest_project.name, oldest_project.index, "/", len(projects))

    # Test with oldest copr commit.
    if not test_with_copr_builds(oldest_project.name, args.test_command):
        # The oldest commit was a 'bad' commit so we can use that as our
        # 'bad' commit for bisecting.
        return git_bisect(repo, args.good_commit, oldest_project.commit, args.test_command)
    else:
        good_project = oldest_project

    # Look for the newest COPR project.
    for p in reversed(projects):
        p.commit = get_clang_commit_for_snapshot_project(p.name, chroot)
        try: 
            repo.git.merge_base('--is-ancestor', p.commit, args.bad_commit)
            newest_project = p
            break
        except:
            continue
    
    print (newest_project.commit, newest_project.name, newest_project.index, "/", len(projects))

    # Test with the newest copr commit
    if test_with_copr_builds(newest_project.name, args.test_command):
        # The newest commit was a 'good' commit, so we can use that as our
        # good commit for testing.
        return git_bisect(repo, newest_project.commit, args.bad_commit, args.test_command)
    else:
        bad_project = newest_project

    # Bisect using copr builds
    while good_project.index + 1 < bad_project.index:
        test_project = projects[(good_project.index + bad_project.index) / 2]
        print(f"Testing: {test_project.name} - {test_project.commit}")
        if test_with_copr_builds(test_project.name, args.test_command):
            print("Good")
            good_project = test_project
        else:
            print("Bad")
            bad_project = test_project

    # Bisect the rest of the way using git.
    return git_bisect(repo, good_project.commit, bad_project.commit, args.test_command)


if __name__ == "__main__":
    main()

