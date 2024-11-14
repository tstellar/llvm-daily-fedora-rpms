import dnf
import hawkey
import re
from typing import Set
import datetime
import copr.v3
import json
import argparse


def filter_llvm_pkgs(pkgs: Set[str]) -> Set[str]:
    llvm_pkgs = ['llvm', 'clang', 'llvm-bolt', 'libomp', 'compiler-rt', 'lld', 'lldb', 'polly', 'libcxx', 'libclc' 'flang', 'mlir']
    filtered = set()
    for p in  pkgs:
        exclude = False
        for l in llvm_pkgs:
            if re.match(l + '[0-9]*$', p):
                exclude = True
                break
        if not exclude:
            filtered.add(p)
    return filtered

"""
This returns a list of packages we don't want to test.
"""
def get_exclusions() -> Set[str]:
    return set()

"""
Tier0 packages are packages in RHEL that use only clang for compiling C/C++ files.

These packages are the top priority for testing.
"""
def get_tier0() -> Set[str]:
    return {'dotnet6.0', 'dotnet7.0', 'dotnet8.0', 'dotnet9.0', 'qemu-kvm', 'golang', 'wasi-lbc'}

"""
Tier 1 packages are any packages in RHEL that have BuildRequires: clang
"""
def get_tier1(exclusions: Set[str] ) -> Set[str]:
    base = dnf.Base()
    conf = base.conf
    for v in ['9', '10']:
        base.repos.add_new_repo(f'buildroot-source-{v}', conf, baseurl=[f'http://download.eng.bos.redhat.com/rhel-{v}/nightly/BUILDROOT-{v}/latest-BUILDROOT-{v}-RHEL-{v}/compose/Buildroot/source/tree/'])
        for c in 'AppStream', 'BaseOS', 'CRB':
            base.repos.add_new_repo(f'{c}-source-{v}', conf, baseurl=[f'http://download.eng.bos.redhat.com/rhel-{v}/nightly/RHEL-{v}/latest-RHEL-{v}/compose/{c}/source/tree/'])
    repos = base.repos.get_matching('*')
    repos.disable()
    repos = base.repos.get_matching('*-source*')
    repos.enable()

    base.fill_sack()
    q = base.sack.query(flags=hawkey.IGNORE_MODULAR_EXCLUDES)
    q = q.available()
    q = q.filter(requires=['clang'])
    pkgs = set([p.name for p in list(q)])
    return filter_llvm_pkgs(pkgs) - exclusions

"""
"""
def get_tier2(exclusions: Set[str]) -> Set[str]:
    base = dnf.Base()
    conf = base.conf
    base.repos.add_new_repo('fedora-source', conf, baseurl=['https://dl.fedoraproject.org/pub/fedora/linux/releases/41/Everything/source/tree/'])
    repos = base.repos.get_matching('*')
    repos.disable()
    repos = base.repos.get_matching('*-source*')
    repos.enable()

    base.fill_sack()
    q = base.sack.query(flags=hawkey.IGNORE_MODULAR_EXCLUDES)
    q = q.available()
    q = q.filter(requires=['clang'])
    pkgs = set([p.name for p in list(q)])
    return filter_llvm_pkgs(pkgs) - exclusions


def get_tier3(exclusions: Set[str]) -> Set[set]:
    base = dnf.Base()
    conf = base.conf
    for c in 'AppStream', 'BaseOS', 'CRB', 'Extras':
         base.repos.add_new_repo(f'{c}-source', conf, baseurl=[f'https://odcs.fedoraproject.org/composes/production/latest-Fedora-ELN/compose/{c}/source/tree/'])
    repos = base.repos.get_matching('*')
    repos.disable()
    repos = base.repos.get_matching('*-source*')
    repos.enable()

    base.fill_sack()
    q = base.sack.query(flags=hawkey.IGNORE_MODULAR_EXCLUDES)
    q = q.available()
    q = q.filter(requires=['clang', 'gcc', 'gcc-c++'])
    pkgs = set([p.name for p in list(q)])
    return filter_llvm_pkgs(pkgs) - exclusions

def get_tier4(exclusions: Set[str]) -> Set[str]:
    base = dnf.Base()
    conf = base.conf
    base.repos.add_new_repo('fedora-source', conf, baseurl=['https://dl.fedoraproject.org/pub/fedora/linux/releases/41/Everything/source/tree/'])
    repos = base.repos.get_matching('*')
    repos.disable()
    repos = base.repos.get_matching('*-source*')
    repos.enable()

    base.fill_sack()
    q = base.sack.query(flags=hawkey.IGNORE_MODULAR_EXCLUDES)
    q = q.available()
    q = q.filter(requires=['clang', 'gcc', 'gcc-c++'])
    pkgs = set([p.name for p in list(q)])
    return filter_llvm_pkgs(pkgs) - exclusions

def get_monthly_rebuild_packages(project_owner: str, project_name: str, copr_client : copr.v3.Client, tier3 : Set[str]) -> Set[str]:
    pkgs = set()
    for p in copr_client.package_proxy.get_list(project_owner, project_name, with_latest_succeeded_build = True, with_latest_build = True):
        latest_succeeded = p['builds']['latest_succeeded']
        latest = p['builds']['latest']
        #print(p)
        #print('before not succed',p['name'], latest_succeeded, latest)
        if not latest_succeeded:
            continue
        #print('before latest', p['name'])
        if latest['id'] != latest_succeeded['id']:
            continue
        #print(p['name'])
        if p['name'] not in tier3:
            continue
        #print(latest)
        pkgs.add(p['name'])
    return pkgs

def get_monthly_rebuild_regressions(project_owner: str, project_name: str, copr_client : copr.v3.Client, start_time: datetime.datetime) -> Set[str]:
    pkgs = []
    for p in copr_client.package_proxy.get_list(project_owner, project_name, with_latest_succeeded_build = True, with_latest_build = True):
        latest_succeeded = p['builds']['latest_succeeded']
        latest = p['builds']['latest']
        if not latest_succeeded:
            continue
        if latest['id'] == latest_succeeded['id']:
            continue
        # latest is a bit a successful build, but this doesn't mean it failed.
        # It could be in progress.
        if latest['state'] != 'failed':
            continue
        if int(latest['submitted_on']) < start_time.timestamp():
            continue
        latest['name'] = p['name']
        pkgs.append({
            'name' : p['name'],
            'url' : f"https://copr.fedorainfracloud.org/coprs/{project_owner}/{project_name}/build/{latest['id']}/"
        })
    return pkgs

def start_rebuild(project_owner: str, project_name: str, copr_client: copr.v3.Client, pkgs: Set[str], snapshot_project_name: str):

    # Update the rebuild project to use the latest snapshot
    copr_client.project_proxy.edit(project_owner, project_name,
                                   additional_repos=['copr://tstellar/fedora-clang-default-cc', f'copr://@fedora-llvm-team/{snapshot_project_name}'])

    buildopts = {
        'background' : True,
    }
    print("Rebuilding", len(pkgs), 'packages')
    pkgs=['zip']
    for p in pkgs:
        print("Rebuild", p)
        copr_client.build_proxy.create_from_distgit(project_owner, project_name,
                                                    p, 'f41', buildopts=buildopts)
        return


def select_snapshot_project(copr_client: copr.v3.Client) -> str:
    project_owner = '@fedora-llvm-team'
    target_chroots = ['fedora-41-aarch64', 'fedora-41-ppc64le', 'fedora-41-s390x', 'fedora-41-x86_64']
    for i in range(14):
        chroots = set()
        day = datetime.date.today() - datetime.timedelta(days = i)
        project_name = day.strftime('llvm-snapshots-big-merge-%Y%m%d')
        print("Trying:", project_name)
        try:
            p = copr_client.project_proxy.get(project_owner, project_name)
            if not p:
                continue
            #print(p)
            pkgs = copr_client.build_proxy.get_list(project_owner, project_name, 'llvm', status='succeeded')
            for pkg in pkgs:
            #    print("PACKAGE")
            #    print(pkg)
                chroots.update(pkg['chroots'])
          
            print(project_name, chroots)
            if all(t in chroots for t in target_chroots):
                print("PASS", project_name)
                return project_name
        except:
            continue
    print("FAIL")
    return None

def main():


    parser = argparse.ArgumentParser()
    parser.add_argument('command', type=str, choices=['rebuild','get-regressions'])
    parser.add_argument('--start-date', type=str, help='Date format: yyyy-mm-dd')

    args = parser.parse_args()


    #print(exclusions)
    #print('Tier 0:', len(tier0), "\n", "\n".join(tier0))
    #print('Tier 1:', len(tier1), "\n", "\n".join(tier1))
    #print('Tier 2:', len(tier2), "\n", "\n".join(tier2))
    #print('Tier 3:', len(tier3), "\n", "\n".join(tier3))
    #print('Tier 4:', len(tier4), "\n", "\n".join(tier4))


    project_owner = 'tstellar'
    project_name = 'fedora-41-clang-19'
    #copr_client = copr.v3.Client()
    copr_client = copr.v3.Client.create_from_config_file()

    if args.command == 'rebuild':
        exclusions = get_exclusions()
        tier0 = get_tier0()
        tier1 = get_tier1(exclusions | tier0)
        tier2 = get_tier2(exclusions | tier0 | tier1)
        tier3 = get_tier3(exclusions | tier0 | tier1 | tier2)
        tier4 = get_tier4(exclusions | tier0 | tier1 | tier2 | tier3)
        pkgs_to_test = get_monthly_rebuild_packages(project_owner, project_name, copr_client, tier0 | tier1 | tier2 | tier3)
        snapshot_project = select_snapshot_project(copr_client)
        start_rebuild(project_owner, project_name, copr_client, pkgs_to_test, snapshot_project)
    elif args.command == 'get-regressions':
        start_time = datetime.datetime.fromisoformat(args.start_date)
        pkg_failures = get_monthly_rebuild_regressions(project_owner, project_name, copr_client, start_time)
        print(json.dumps(pkg_failures))



if __name__ == '__main__':
    main()
