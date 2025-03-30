#!/bin/bash

set -ex

function get_clang_commit {
  buildid=$1
  pkg=$2

  curl "https://download.copr.fedorainfracloud.org/results/@fedora-llvm-team/fedora-41-clang-21/fedora-41-x86_64/0$buildid-$pkg/root.log.gz" | gunzip |  grep -o 'clang[[:space:]]\+x86_64[[:space:]]\+[0-9a-g~pre.]\+' | cut -d 'g' -f 3
}

function get_clang_copr_project {
  buildid=$1
  pkg=$2
  arch=$(rpm --eval %{_arch})

  date=$(curl "https://download.copr.fedorainfracloud.org/results/@fedora-llvm-team/fedora-41-clang-21/fedora-41-$arch/0$buildid-$pkg/root.log.gz" | gunzip |  grep -o "clang[[:space:]]\+$arch[[:space:]]\+[0-9.]\+~pre[0-9]\+" | cut -d '~' -f 2 | sed 's/pre//g')
  echo "@fedora-llvm-team/llvm-snapshots-big-merge-$date"
}

function configure_llvm {
  cmake -G Ninja -B build -S llvm -DCMAKE_BUILD_TYPE=Release -DLLVM_ENABLE_PROJECTS=clang -DLLVM_TARGETS_TO_BUILD=Native -DLLVM_BINUTILS_INCDIR=/usr/include/ -DCMAKE_CXX_COMPILER_LAUNCHER=ccache 

}

function copr_project_exists {
  project=$1
  owner=$(echo $project | cut -d '/' -f 1)
  name=$(echo $project | cut -d '/' -f 2)

  curl -f -X 'GET' "https://copr.fedorainfracloud.org/api_3/project/?ownername=$owner&projectname=$name"   -H 'accept: application/json'
}


pkg_or_buildid=$1

if echo $pkg_or_buildid | grep '^[0-9]\+'; then
  buildid=$pkg_or_buildid
  read -r pkg last_success_id <<<$(curl -X 'GET' "https://copr.fedorainfracloud.org/api_3/build/$buildid" -H 'accept: application/json' | jq -r '[.builds.latest.source_package.name,.builds.latest_succeeded.id] |  join(" ")')
else
  pkg=$pkg_or_buildid
fi

read -r buildid last_success_id <<<$(curl -X 'GET' \
  "https://copr.fedorainfracloud.org/api_3/package/?ownername=%40fedora-llvm-team&projectname=fedora-41-clang-21&packagename=$pkg&with_latest_build=true&with_latest_succeeded_build=true" \
  -H 'accept: application/json' | jq -r '[.builds.latest.id,.builds.latest_succeeded.id] | join(" ")' )


good_commit=llvmorg-20-init
bad_commit=origin/main

good_commit=$(get_clang_commit $last_success_id $pkg)
bad_commit=$(get_clang_commit $buildid $pkg)

srpm_url=$(curl -X 'GET' "https://copr.fedorainfracloud.org/api_3/build/$buildid" -H 'accept: application/json' | jq -r .source_package.url)
curl -O -L $srpm_url
srpm_name=$(basename $srpm_url)

dnf builddep -y $srpm_name

# Test the good commit to see if this a false positive
good_copr_project=$(get_clang_copr_project $last_success_id $pkg)
if copr_project_exists $good_copr_project; then
  dnf copr enable -y $good_copr_project
  dnf install -y clang
  dnf reinstall -y clang
  if ! rpmbuild -D '%toolchain clang' -rb $srpm_name; then
    echo "False Positive."
    exit 1
  fi
else
  #git checkout $good_commit
  #configure_llvm
  #if ! ./git-bisect-script.sh $srpm_name; then
  #  echo "False Positive."
  #  exit 1
  #fi
  true
fi

bad_copr_project=$(get_clang_copr_project $buildid $pkg)
if copr_project_exists $bad_copr_project; then
  dnf copr enable -y $bad_copr_project
  dnf install -y clang
  dnf reinstall -y clang
  if rpmbuild -D '%toolchain clang' -rb $srpm_name; then
    echo "False Positive."
    #exit 1
  fi
else
  #git checkout $bad_commit
  #configure_llvm
  # Test the bad commit to see if this a false positive
  #if ./git-bisect-script.sh $srpm_name; then
  #  echo "False Positive."
  #  exit 1
  #fi
  true
fi

# First attempt to bisect using prebuilt binaries.
chroot="fedora-41-$(rpm --eval %{arch})"
good=$good_copr_project
bad=$bad_copr_project

while [ True ]; do
  test_project=$(python3 rebuilder.py bisect --chroot $chroot $good $bad)

  if [ "$test_project" = "$good_copr_project" ] || [ "$test_project" = "$bad_copr_project" ]; then
    break
  fi

  dnf copr enable -y $test_project
  dnf install -y clang
  dnf reinstall -y clang
  if rpmbuild -D '%toolchain clang' -rb $srpm_name; then
    good=$test_project
  else
    bad=$test_project
  fi
done

if copr_project_exists $good; then
    dnf copr enable -y $good
    dnf install -y clang
    dnf reinstall -y clang
    good_commit=$(dnf info --installed clang | grep '^Version' | cut -d 'g' -f 2)
fi

if copr_project_exists $bad; then
    dnf copr enable -y $bad
    dnf install -y clang
    dnf reinstall -y clang
    bad_commit=$(dnf info --installed clang | grep '^Version' | cut -d 'g' -f 2)
fi

git bisect start
git bisect good $good_commit
git bisect bad $bad_commit

configure_llvm
git bisect run ./git-bisect-script.sh $srpm_name
