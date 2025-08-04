import argparse
import re
import copr.v3
import git
import dnf
import dnf.cli
import sys
import subprocess
import tempfile

class CoprProject:
    UNTESTED = 0
    GOOD = 1
    BAD = 2

    def __init__(self, name, commit):
        self.name = name
        self.index = -1
        self.commit = commit
        self._status = CoprProject.UNTESTED

    def __lt__(self, other):
        return self.name < other.name


def get_snapshot_projects(chroot: str) -> list[str]:
    copr_client = copr.v3.Client.create_from_config_file()
    projects = []
    for p in copr_client.project_proxy.get_list(ownername='@fedora-llvm-team'):
        if not re.match(r"llvm-snapshots-big-merge-[0-9]+", p.name):
            continue
        if chroot and chroot not in list(p.chroot_repos.keys()):
            continue
        new_project = CoprProject(p.name, get_clang_commit_for_snapshot_project(p.name, chroot))
        if not new_project.commit:
            continue
        projects.append(new_project)
    projects.sort()
    for idx, p in enumerate(projects):
        p.index = idx 
    return projects


def get_clang_commit_for_snapshot_project(project_name: str, chroot: str) -> str:
    copr_client = copr.v3.Client.create_from_config_file()

    builds = copr_client.build_proxy.get_list('@fedora-llvm-team', project_name, packagename="llvm", status="succeeded")
    print(f"Gettting commit for {project_name}")
    regex = re.compile("llvm-[0-9.]+~pre[0-9]+.g([0-9a-f]+)")
    for  b in builds:
        if chroot in b["chroots"]:
            print("Regex name: ", b["source_package"]["url"])
            m = regex.search(b["source_package"]["url"])
            if m:
                return m.group(1)
    # Usually this means that there was no succesful build for this chroot.
    print(f"Can't find commit for {project_name}")
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
    subprocess.run(["dnf", "copr", "enable", "-y", copr_fullname])
    # Install clang and llvm builds to test
    dnf.conf.Conf.best = True
    with dnf.Base() as base:
        base.read_all_repos()
        base.fill_sack()
        for r in rpms:
            base.install(r)
        base.resolve(allow_erasing=True)
        print(base.transaction.install_set)
        base.download_packages(base.transaction.install_set)
        base.do_transaction()

    # Disable project so future installs don't use it.
    # FIXME: There is probably some way to do this via the python API, but I
    # can't figure it out.
    subprocess.run(["dnf", "copr", "disable", "-y", copr_fullname])

    print(test_command)
    #test_command = "git -C /root/llvm-project merge-base --is-ancestor HEAD 6cac792bf9eacb1ed0c80fc7c767fc99c50e252"
    print(test_command)
    print(test_command.split())
    p = subprocess.run(test_command, shell=True)
    print(p)
    success = True if p.returncode == 0 else False
    print("{}: {}".format(copr_project, "Good" if success else "Bad"))
    return success

def git_bisect(repo: git.Repo, good_commit: str, bad_commit: str, configure_command: str, build_command: str, test_command: str):
    print(f"Running git bisect with {good_commit} and {bad_commit}")
    print(configure_command)
    print(build_command)
    print(test_command)

    # Configure llvm
    subprocess.run(configure_command.split(), cwd = repo.working_tree_dir)

    # Use subprocess.run here instead of builtin commands so we can stream output.
    subprocess.run(["git", "-C", repo.working_tree_dir, "bisect", "start", bad_commit, good_commit])
    with tempfile.NamedTemporaryFile(mode='w+', delete = False) as bisect_script:
        bisect_script.write(f"""
            set -ex
            pwd
            echo "Trying build command"
            if ! {build_command}; then
              echo "exit 125"
              exit 125
            fi
            {test_command}
        """)
        bisect_script.flush()
        # Use the cwd argument instead of passing -C to git, so that the bisect script is
        # run in the llvm-project directory.
        #os.chdir(repo.working_tree_dir)
        print(repo.working_tree_dir)
        subprocess.run(["git", "bisect", "run", "/usr/bin/bash", "--verbose", bisect_script.name], cwd = repo.working_tree_dir)
    print(repo.git.bisect("log"))
    return True


def main():

    parser = argparse.ArgumentParser()
    parser.add_argument('--good-commit')
    parser.add_argument('--bad-commit')
    parser.add_argument('--llvm-project-dir')
    parser.add_argument('--configure-command', default = "cmake -S llvm -G Ninja -B build -DCMAKE_BUILD_TYPE=Release -DLLVM_TARGETS_TO_BUILD=Native -DLLVM_ENABLE_PROJECTS=clang -DLLVM_BINUTILS_INCDIR=/usr/include/ -DENABLE_LINKER_BUILD_ID=ON")
    parser.add_argument('--build-command', default = "ninja -C build install-clang install-clang-resource-headers install-LLVMgold install-llvm-ar install-llvm-ranlib")
    parser.add_argument('--test-command')
    parser.add_argument('--srpm')
    parser.add_argument('--chroot')
    args = parser.parse_args()

    repo = git.Repo(args.llvm_project_dir)

    chroot = args.chroot
    projects = get_snapshot_projects(chroot)
    good_project = None
    bad_project = None

    # Find for the oldest COPR project that is newer than the good commit.
    for p in projects:
        try: 
            repo.git.merge_base('--is-ancestor', args.good_commit, p.commit)
        except:
            continue
        print(p.commit, p.name, p.index, "/", len(projects))

        if not test_with_copr_builds(p.name, args.test_command):
            # The oldest commit was a 'bad' commit so we can use that as our
            # 'bad' commit for bisecting.
            return git_bisect(repo, args.good_commit, p.commit, args.configure_command, args.build_command, args.test_command)
        good_project = p
        break

    # Find the newest COPR project that is older than the bad commit.
    for p in reversed(projects):
        try: 
            repo.git.merge_base('--is-ancestor', p.commit, args.bad_commit)
        except:
            continue
        print(p.commit, p.name, p.index, "/", len(projects))
        
        # We found a project, so test it.
        if test_with_copr_builds(p.name, args.test_command):
            # The newest commit was a 'good' commit, so we can use that as our
            # good commit for testing.
            return git_bisect(repo, p.commit, p.bad_commit, args.configure_command, args.build_command, args.test_command)
        bad_project = p
        break


    # Bisect using copr builds
    if good_project and bad_project:
        while good_project.index + 1 < bad_project.index:
            test_project = projects[int((good_project.index + bad_project.index) / 2)]
            print(f"Testing: {test_project.name} - {test_project.commit}")
            if test_with_copr_builds(test_project.name, args.test_command):
                print("Good")
                good_project = test_project
            else:
                print("Bad")
                bad_project = test_project
    if good_project:
        args.good_commit = good_project.commit
    if bad_project:
        args.bad_commit = bad_project.commit

    # Bisect the rest of the way using git.
    return git_bisect(repo, args.good_commit, args.bad_commit, args.configure_command, args.build_command, args.test_command)


if __name__ == "__main__":
    main()

