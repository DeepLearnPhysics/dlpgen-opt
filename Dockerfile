# LArCV2 supplies the ROOT/PyROOT and output-I/O layer required by edep2supera.
# The tag is immutable at the release level; production deployments should also
# pin the resolved base-image digest in their build metadata.
FROM ghcr.io/deeplearnphysics/larcv2:2.4.1-ubuntu22.04@sha256:2a685aa58041e0fe81a4d23d119cda52a7c39db70709ce0871039fe35af0f6f8 AS larcv_base

# NEUT 5.8.0 is distributed through its public quickstart image. Extract only
# the event generator, NuHepMC converter, data tables, and their private ROOT
# 6.34 runtime. This stage is selected per target architecture by the pinned
# multi-platform image index. The final runtime never adds these libraries to
# its global environment, because the rest of the stack uses ROOT 6.32.
FROM picker24/neut580_quickstart@sha256:ccb82172b5f202382bece34bf60e3affcd31f3baf26f1ea43bd039b2552f1398 AS neut_upstream

RUN mkdir -p /opt/neut-export/neut/bin /opt/neut-export/neut/lib \
        /opt/neut-export/neut/share /opt/neut-export/root \
        /opt/neut-root-share /opt/neut-root-include \
        /opt/neut-source/neutclass \
        /opt/neut-export/hepmc/lib64 /opt/neut-export/nuhepmc/lib \
        /opt/neut-export/buildbox/lib64 /opt/neut-export/system \
    && cp /opt/neut/5.8.0/bin/neutroot2 \
        /opt/neut/5.8.0/bin/neutvect-converter /opt/neut-export/neut/bin/ \
    && cp -a /opt/neut/5.8.0/lib/libNEUTClass.so* \
        /opt/neut/5.8.0/lib/libnvconv.so \
        /opt/neut/5.8.0/lib/neutclassDict_rdict.pcm \
        /opt/neut-export/neut/lib/ \
    && cp -a /opt/neut/5.8.0/share/. /opt/neut-export/neut/share/ \
    && cp -a /usr/lib64/root/. /opt/neut-export/root/ \
    && cp -a /usr/share/root/. /opt/neut-root-share/ \
    && cp -a /usr/include/root/. /opt/neut-root-include/ \
    && cp -a /opt/neut/5.8.0/src/neutclass/*.h \
        /opt/neut-source/neutclass/ \
    && cp -a /opt/HepMC3/3.3.2/lib64/libHepMC3.so* \
        /opt/HepMC3/3.3.2/lib64/libHepMC3protobufIO.so* \
        /opt/neut-export/hepmc/lib64/ \
    && cp -a /opt/NuHepMC_CPPUtils/git_master/lib/libnuhepmc_cpputils.so \
        /opt/neut-export/nuhepmc/lib/ \
    && cp -a /opt/buildbox/lib64/libspdlog.so* \
        /opt/buildbox/lib64/libfmt.so* /opt/neut-export/buildbox/lib64/ \
    && cp -a /usr/lib64/libprotobuf.so.25* /usr/lib64/liburing.so.2* \
        /opt/neut-export/system/ \
    && test "$(git -C /opt/neut/5.8.0 rev-parse HEAD)" = \
        c3f9e4e0c19512e0ed16bfbe50e5807a0da7164b

FROM larcv_base

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG DEBIAN_FRONTEND=noninteractive
ARG GEANT4_VERSION=11.4.2
ARG BUILD_JOBS=2
ARG PYTHIA8_VERSION=8317
ARG GENIE_VERSION=3.6.2
ARG GENIE_TUNE=AR23_20i_00_000

LABEL org.opencontainers.image.source="https://github.com/DeepLearnPhysics/dlpgen-opt" \
      org.opencontainers.image.description="DLPGenerator to edep-sim to Supera production runtime"

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        cmake \
        curl \
        g++ \
        git \
        libexpat1-dev \
        libhdf5-dev \
        liblog4cpp5-dev \
        libgsl-dev \
        libxml2-dev \
        libxerces-c-dev \
        make \
        ninja-build \
        python3-dev \
        python3-pip \
        rsync \
        xz-utils \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Build Geant4 with the GDML and datasets required by edep-sim. The upstream
# version is explicit and therefore appears in the final image metadata.
RUN curl -fL \
      "https://gitlab.cern.ch/geant4/geant4/-/archive/v${GEANT4_VERSION}/geant4-v${GEANT4_VERSION}.tar.gz" \
      -o /tmp/geant4.tar.gz \
    && mkdir -p /opt/geant4-source \
    && tar -xzf /tmp/geant4.tar.gz -C /opt/geant4-source --strip-components=1 \
    && rm /tmp/geant4.tar.gz

# Keep the scratch build tree in a cache mount for retries on one builder, but
# compile and install atomically. External BuildKit cache exporters do not
# preserve cache-mount contents; the completed /opt/geant4 installation must be
# part of the exported layer for a fresh release runner to restore it safely.
RUN --mount=type=cache,id=dlpgen-opt-geant4-${GEANT4_VERSION},target=/tmp/geant4-build \
    cmake -S /opt/geant4-source -B /tmp/geant4-build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/opt/geant4 \
        -DGEANT4_BUILD_MULTITHREADED=ON \
        -DGEANT4_INSTALL_DATA=ON \
        -DGEANT4_USE_GDML=ON \
        -DGEANT4_USE_OPENGL_X11=OFF \
        -DGEANT4_USE_QT=OFF \
    && cmake --build /tmp/geant4-build --parallel "${BUILD_JOBS}" \
    && cmake --install /tmp/geant4-build > /tmp/geant4-install.log \
    && test -x /opt/geant4/bin/geant4.sh \
    && rm -rf /opt/geant4-source

# Current edep-sim requires CMake >=3.30, newer than Ubuntu 22.04's package.
# Install it only after Geant4 so changing this tool pin cannot invalidate the
# expensive physics-toolkit build layers.
ARG CMAKE_VERSION=3.31.6
RUN apt-get update \
    && apt-get install -y --no-install-recommends libyaml-cpp-dev \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m pip install --no-cache-dir "cmake==${CMAKE_VERSION}" \
    && cmake --version

# Build the official Pythia 8.317 release explicitly and pin its source archive.
ARG PYTHIA8_SOURCE_SHA256=a93337111927568503f68a5266c45dca79d461e56b74639efc1d2af2ee87c021
RUN curl -fL \
      "https://gitlab.com/Pythia8/releases/-/archive/pythia${PYTHIA8_VERSION}/releases-pythia${PYTHIA8_VERSION}.tar.gz" \
      -o /tmp/pythia8.tar.gz \
    && echo "${PYTHIA8_SOURCE_SHA256}  /tmp/pythia8.tar.gz" | sha256sum -c - \
    && mkdir -p /tmp/pythia8-source \
    && tar -xzf /tmp/pythia8.tar.gz -C /tmp/pythia8-source --strip-components=1 \
    && cd /tmp/pythia8-source \
    && ./configure --prefix=/opt/pythia8 --with-gzip \
    && make -j"${BUILD_JOBS}" \
    && make install \
    && test -f /opt/pythia8/lib/libpythia8.so \
    && rm -rf /tmp/pythia8-source /tmp/pythia8.tar.gz

# ROOT removed TPythia6 in 6.30. Build the version-pinned standalone extraction
# before GENIE so one GENIE installation can expose both Pythia backends. NuWro
# consumes the same adapter. Do not retain -march=native in release images.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nlohmann-json3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY dependencies/ROOTEGPythia6 /usr/share/source/rootegpythia6
RUN --mount=type=cache,id=dlpgen-opt-rootegpythia6,target=/tmp/rootegpythia6-build \
    sed -i 's/target_compile_options(Pythia6 PRIVATE -march=native)/target_compile_options(Pythia6 PRIVATE)/' \
        /usr/share/source/rootegpythia6/CMakeLists.txt \
    && cmake -S /usr/share/source/rootegpythia6 \
        -B /tmp/rootegpythia6-build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/opt/rootegpythia6 \
        -DROOTEGPythia6_Pythia6_BUILTIN=ON \
    && cmake --build /tmp/rootegpythia6-build --parallel "${BUILD_JOBS}" \
    && cmake --install /tmp/rootegpythia6-build \
    && test -f /opt/rootegpythia6/lib/libPythia6.so \
    && test -f /opt/rootegpythia6/lib/libEGPythia6.so

# Keep GENIE and dk2nu as pinned source submodules, but copy them into
# temporary build locations so their object files do not inflate the runtime
# dependency tree retained under /opt/dlpgen-opt.
COPY dependencies/GENIE /tmp/genie-pristine
COPY docker/patches/genie-dkmeta-output.patch /tmp/genie-dkmeta-output.patch
ENV GENIE=/opt/genie \
    PYTHIA8=/opt/pythia8 \
    PYTHIA8DATA=/opt/pythia8/share/Pythia8/xmldoc \
    ROOTEGPythia6_ROOT=/opt/rootegpythia6 \
    PYTHIA6=/opt/rootegpythia6/lib \
    LD_LIBRARY_PATH=/opt/rootegpythia6/lib:/opt/pythia8/lib:${LD_LIBRARY_PATH}
# HEDIS is an ultra-high-energy module and unconditionally requires LHAPDF
# even in GENIE 3.6.2 builds configured with LHAPDF disabled. It is irrelevant
# to accelerator-beam energies, so omit it and the structure-function utilities
# that require LHAPDF rather than carrying LHAPDF and PDF datasets.
ARG GENIE_BUILD_VARIANT=dual-pythia-v1
RUN --mount=type=cache,id=dlpgen-opt-genie-${GENIE_VERSION}-dual-pythia-v1,target=/tmp/genie-source \
    test "${GENIE_BUILD_VARIANT}" = dual-pythia-v1 \
    && rsync -a /tmp/genie-pristine/ /tmp/genie-source/ \
    && git -C /tmp apply --no-index --directory=genie-source \
        /tmp/genie-dkmeta-output.patch \
    && sed -i '/Physics\/HEDIS/d' /tmp/genie-source/Makefile \
    && sed -i '/^TGT_BASE =/,/^$/ { /gmkhedissf/d; /gcalchedisdiffxsec/d; /gmkphotonsf/d; }' \
        /tmp/genie-source/src/Apps/Makefile \
    && sed -i 's/ -lGPhHEDISXS -lGPhHEDISEG//' \
        /tmp/genie-source/src/scripts/setup/genie-config \
    && sed -i 's|^PY6ROOT_LIBRARY = -lEGPythia6$|PY6ROOT_LIBRARY = -L$(PYTHIA6_DIR) -lEGPythia6|' \
        /tmp/genie-source/src/make/Make.include \
    && sed -i 's|^ROOT_INCLUDES  = -I$(shell root-config --incdir)$|ROOT_INCLUDES  = -I$(shell root-config --incdir) $(PYTHIA6_INCLUDES)|' \
        /tmp/genie-source/src/make/Make.include \
    && cd /tmp/genie-source \
    && GENIE=/tmp/genie-source ./configure \
        --prefix=/opt/genie \
        --enable-pythia6 \
        --with-pythia6-lib=/opt/rootegpythia6/lib \
        --enable-pythia8 \
        --with-pythia8-inc=/opt/pythia8/include \
        --with-pythia8-lib=/opt/pythia8/lib \
        --disable-lhapdf5 \
        --disable-lhapdf6 \
        --enable-flux-drivers \
        --enable-geom-drivers \
        --enable-fnal \
    && GENIE=/tmp/genie-source make -j"${BUILD_JOBS}" \
        PYTHIA6_INCLUDES=-I/opt/rootegpythia6/include \
    && (GENIE=/tmp/genie-source make install \
        PYTHIA6_INCLUDES=-I/opt/rootegpythia6/include \
        > /tmp/genie-install.log 2>&1 \
        || { tail -n 120 /tmp/genie-install.log; exit 1; }) \
    && cp -a /tmp/genie-source/config /tmp/genie-source/data \
        /tmp/genie-source/VERSION /opt/genie/ \
    && ln -s include/GENIE /opt/genie/src \
    && test -x /opt/genie/bin/gevgen_fnal \
    && test -x /opt/genie/bin/gntpc \
    && test -d /opt/genie/src/Framework

# Preserve GENIE's Pythia6 defaults and publish a Pythia8 overlay. The runtime
# selects exactly one overlay through GXMLPATH for each generator process.
RUN mkdir -p /opt/genie/config/hadronization/pythia6 \
        /opt/genie/config/hadronization/pythia8 \
    && cp /opt/genie/config/UnstableParticleDecayer.xml \
        /opt/genie/config/AGKYLowW2019.xml \
        /opt/genie/config/AGKY2019.xml \
        /opt/genie/config/DISHadronicSystemGenerator.xml \
        /opt/genie/config/hadronization/pythia6/ \
    && cp /opt/genie/config/UnstableParticleDecayer.xml \
        /opt/genie/config/AGKYLowW2019.xml \
        /opt/genie/config/AGKY2019.xml \
        /opt/genie/config/DISHadronicSystemGenerator.xml \
        /opt/genie/config/hadronization/pythia8/ \
    && sed -i \
        -e 's/genie::Pythia6Decayer2023/genie::Pythia8Decayer2023/g' \
        -e 's/genie::Pythia6Hadro2019/genie::Pythia8Hadro2019/g' \
        -e 's/genie::AGCharmPythia6Hadro2023/genie::AGCharmPythia8Hadro2023/g' \
        /opt/genie/config/hadronization/pythia8/*.xml \
    && ! grep -Eq 'genie::(Pythia6Decayer2023|Pythia6Hadro2019|AGCharmPythia6Hadro2023)' \
        /opt/genie/config/hadronization/pythia8/*.xml \
    && grep -Eq 'genie::(Pythia6Decayer2023|Pythia6Hadro2019|AGCharmPythia6Hadro2023)' \
        /opt/genie/config/hadronization/pythia6/*.xml

COPY dependencies/dk2nu /tmp/dk2nu-source
RUN --mount=type=cache,id=dlpgen-opt-dk2nu,target=/tmp/dk2nu-build \
    GENIE=/opt/genie \
    GENIE_LIB=/opt/genie/lib \
    LIBXML2_INC=/usr/include/libxml2 \
    LIBXML2_FQ_DIR=/usr \
    LOG4CPP_INC=/usr/include \
    LOG4CPP_LIB=/usr/lib/x86_64-linux-gnu \
    cmake -S /tmp/dk2nu-source -B /tmp/dk2nu-build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/opt/dk2nu \
        -DXML2=/usr/lib/x86_64-linux-gnu/libxml2.so \
        -DWITH_GENIE=ON \
        -DWITH_TBB=OFF \
        -DCOPY_AUX=ON \
    && GENIE=/opt/genie \
       GENIE_LIB=/opt/genie/lib \
       cmake --build /tmp/dk2nu-build --parallel "${BUILD_JOBS}" \
    && cmake --install /tmp/dk2nu-build \
    && test -f /opt/dk2nu/lib/libdk2nuTree.so \
    && test -f /opt/dk2nu/lib/libdk2nuGenie.so \
    && rm -rf /tmp/dk2nu-source /tmp/genie-pristine

# Install the reduced FNAL spline tables used by the SBN/DUNE liquid-argon
# baseline, the matched G18 hA/hN FSI comparison, and the N24 correlated-tail
# variant. Transport archives are deleted after their runtime XML table is
# extracted. G18_10a and G18_10b differ only in the post-interaction FSI model,
# so they share the G18_10a primary-interaction cross-section spline published
# by Fermilab.
ARG GENIE_AR23_XSEC_SHA256=0db236612dad273d90969fdf4e98d277dcdee0ec07a58c16107efa8df43157df
RUN curl -fL \
      "https://scisoft.fnal.gov/scisoft/packages/genie_xsec/v3_06_02_sbn2/genie_xsec-3.06.02.sbn2-noarch-AR2320i00000-k250-e1000.tar.bz2" \
      -o /tmp/genie-xsec.tar.bz2 \
    && echo "${GENIE_AR23_XSEC_SHA256}  /tmp/genie-xsec.tar.bz2" | sha256sum -c - \
    && mkdir -p /opt/genie/xsec \
    && tar -xjf /tmp/genie-xsec.tar.bz2 -C /tmp \
        genie_xsec/v3_06_02_sbn2/NULL/AR2320i00000-k250-e1000/data/gxspl-NUsmall.xml \
    && mv /tmp/genie_xsec/v3_06_02_sbn2/NULL/AR2320i00000-k250-e1000/data/gxspl-NUsmall.xml \
        /opt/genie/xsec/gxspl-AR23_20i_00_000.xml \
    && rm -rf /tmp/genie_xsec /tmp/genie-xsec.tar.bz2

ARG GENIE_N24_XSEC_SHA256=ea17249e6bb3159b27bb865e2a6a0133c3ba20acc8b0232ef8e82d88c40440af
RUN curl -fL \
      "https://scisoft.fnal.gov/scisoft/packages/genie_xsec/v3_06_00/genie_xsec-3.06.00-noarch-N2420i0211b-k250-e1000.tar.bz2" \
      -o /tmp/genie-xsec.tar.bz2 \
    && echo "${GENIE_N24_XSEC_SHA256}  /tmp/genie-xsec.tar.bz2" | sha256sum -c - \
    && tar -xjf /tmp/genie-xsec.tar.bz2 -C /tmp \
        genie_xsec/v3_06_00/NULL/N2420i0211b-k250-e1000/data/gxspl-NUsmall.xml \
    && mv /tmp/genie_xsec/v3_06_00/NULL/N2420i0211b-k250-e1000/data/gxspl-NUsmall.xml \
        /opt/genie/xsec/gxspl-N24_20i_02_11b.xml \
    && rm -rf /tmp/genie_xsec /tmp/genie-xsec.tar.bz2

ARG GENIE_G18_10A_XSEC_SHA256=9da92ba6410c5b5eb08018c2699daa8022799c52aba977946997246cea8f7e0a
RUN curl -fL \
      "https://scisoft.fnal.gov/scisoft/packages/genie_xsec/v3_06_02_sbn2/genie_xsec-3.06.02.sbn2-noarch-G1810a0211b-k250-e1000.tar.bz2" \
      -o /tmp/genie-xsec.tar.bz2 \
    && echo "${GENIE_G18_10A_XSEC_SHA256}  /tmp/genie-xsec.tar.bz2" | sha256sum -c - \
    && tar -xjf /tmp/genie-xsec.tar.bz2 -C /tmp \
        genie_xsec/v3_06_02_sbn2/NULL/G1810a0211b-k250-e1000/data/gxspl-NUsmall.xml \
    && mv /tmp/genie_xsec/v3_06_02_sbn2/NULL/G1810a0211b-k250-e1000/data/gxspl-NUsmall.xml \
        /opt/genie/xsec/gxspl-G18_10a_02_11b.xml \
    && cp /opt/genie/xsec/gxspl-G18_10a_02_11b.xml \
        /opt/genie/xsec/gxspl-G18_10b_02_11b.xml \
    && sed -i 's/G18_10a_02_11b/G18_10b_02_11b/g' \
        /opt/genie/xsec/gxspl-G18_10b_02_11b.xml \
    && rm -rf /tmp/genie_xsec /tmp/genie-xsec.tar.bz2

# Package GiBUU's native NuHepMC producer and matching input tables. GiBUU is
# GPL-2.0, so retain its exact source archive and license in the distributed
# image alongside the executable.
ARG GIBUU_RELEASE=2025
ARG GIBUU_SOURCE_SHA256=bed77e069e657254a2e474d304722f568e57c3b4591559c5d132680c83fa3eed
ARG GIBUU_INPUT_SHA256=99a5fee2abc7648e69a0fa3a102b1c9e8450e92995c164c6d0ccaeeffd16d067
RUN apt-get update \
    && apt-get install -y --no-install-recommends libbz2-dev \
    && rm -rf /var/lib/apt/lists/* \
    && command -v gfortran \
    && curl -fL --retry 5 --retry-delay 2 --retry-all-errors \
      "https://gibuu.hepforge.org/downloads?f=release${GIBUU_RELEASE}.tar.gz" \
      -o /tmp/gibuu-source.tar.gz \
    && curl -fL --retry 5 --retry-delay 2 --retry-all-errors \
      "https://gibuu.hepforge.org/downloads?f=buuinput${GIBUU_RELEASE}.tar.gz" \
      -o /tmp/gibuu-input.tar.gz \
    && echo "${GIBUU_SOURCE_SHA256}  /tmp/gibuu-source.tar.gz" | sha256sum -c - \
    && echo "${GIBUU_INPUT_SHA256}  /tmp/gibuu-input.tar.gz" | sha256sum -c - \
    && mkdir -p /tmp/gibuu-source /opt/gibuu/jobcards /usr/share/source/gibuu \
        /usr/share/licenses/gibuu \
    && tar -xzf /tmp/gibuu-source.tar.gz -C /tmp/gibuu-source \
        --strip-components=1 \
    && tar -xzf /tmp/gibuu-input.tar.gz -C /opt/gibuu \
    && make -C /tmp/gibuu-source FORT=gfortran MODE=opt -j"${BUILD_JOBS}" \
    && cp -L /tmp/gibuu-source/testRun/GiBUU.x /opt/gibuu/GiBUU.x \
    && cp /tmp/gibuu-source/testRun/jobCards/005_testOutputToNuHepMC.job \
        /opt/gibuu/jobcards/argon40-nuhepmc.job \
    && cp /tmp/gibuu-source/LICENSE /usr/share/licenses/gibuu/LICENSE \
    && cp /tmp/gibuu-source/version.txt /opt/gibuu/version.txt \
    && cp /tmp/gibuu-source.tar.gz \
        /usr/share/source/gibuu/release${GIBUU_RELEASE}.tar.gz \
    && test -x /opt/gibuu/GiBUU.x \
    && test -d /opt/gibuu/buuinput \
    && test -s /opt/gibuu/jobcards/argon40-nuhepmc.job \
    && rm -rf /tmp/gibuu-source /tmp/gibuu-source.tar.gz \
        /tmp/gibuu-input.tar.gz

# Base physics settings on the official FSI-enabled SBND jobcard. Keep the
# upstream NuHepMC test card above as a reference, but do not use its
# numTimeSteps=0 software-test configuration for production.
RUN tar -xOf /usr/share/source/gibuu/release${GIBUU_RELEASE}.tar.gz \
      release${GIBUU_RELEASE}/testRun/jobCards/005_Neutrino_SBND_nu.job \
      > /opt/gibuu/jobcards/sbnd-argon40-nuhepmc.job \
    && test -s /opt/gibuu/jobcards/sbnd-argon40-nuhepmc.job

# Keep the exact GPL-3.0 NuWro checkout in /opt/nuwro alongside its installed
# executable, event dictionary, input tables, and native conversion utility.
ARG NUWRO_VERSION=25.11.1
COPY dependencies/NuWro /opt/nuwro
COPY docker/patches/nuwro-root632.patch /tmp/nuwro-root632.patch
RUN --mount=type=cache,id=dlpgen-opt-nuwro-${NUWRO_VERSION},target=/tmp/nuwro-build \
    apt-get update \
    && apt-get install -y --no-install-recommends libxml2-utils \
    && rm -rf /var/lib/apt/lists/* \
    && git -C /opt apply --no-index --directory=nuwro \
        /tmp/nuwro-root632.patch \
    && sed -i \
        -e 's/eel_theta_lab/el_costh_lab/g' \
        -e 's/eel_dz/el_costh_del/g' \
        /opt/nuwro/src/e_el_event.cc /opt/nuwro/src/e_spp_event.cc \
    && PYTHIA6=/opt/rootegpythia6/lib \
       cmake -S /opt/nuwro -B /tmp/nuwro-build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DDLPGEN_NUWRO_VERSION="NuWro_${NUWRO_VERSION//./_}" \
        -DNUWRO_CFLAGS=-I/opt/rootegpythia6/include \
    && cmake --build /tmp/nuwro-build --parallel "${BUILD_JOBS}" \
    && cmake --install /tmp/nuwro-build \
    && test -x /opt/nuwro/bin/nuwro \
    && test -x /opt/nuwro/bin/nuwro2rootracker \
    && test -f /opt/nuwro/lib/libevent.so \
    && test -d /opt/nuwro/data

WORKDIR /opt/dlpgen-opt
COPY dependencies/DLPGenerator /opt/dlpgen-opt/dependencies/DLPGenerator
COPY dependencies/edep-sim /opt/dlpgen-opt/dependencies/edep-sim
COPY dependencies/SuperaAtomic /opt/dlpgen-opt/dependencies/SuperaAtomic
COPY dependencies/edep2supera /opt/dlpgen-opt/dependencies/edep2supera

ENV EDEPSIM_ROOT=/opt/edep-sim \
    DLPGENERATOR_DIR=/opt/dlpgen-opt/dependencies/DLPGenerator \
    DLPGENERATOR_BINDIR=/opt/dlpgen-opt/dependencies/DLPGenerator/bin \
    DLPGENERATOR_BUILDDIR=/opt/dlpgen-opt/dependencies/DLPGenerator/build \
    DLPGENERATOR_LIBDIR=/opt/dlpgen-opt/dependencies/DLPGenerator/build/lib \
    DLPGENERATOR_INCDIR=/opt/dlpgen-opt/dependencies/DLPGenerator/build/include \
    DLPGENERATOR_CXX=g++ \
    DLPGENERATOR_CXXSTDFLAG=-std=c++17 \
    SUPERA_WITH_PYROOT=True \
    SUPERA_WITH_PYBIND=False \
    PIP_BREAK_SYSTEM_PACKAGES=1

RUN source /opt/geant4/bin/geant4.sh \
    && cmake -S dependencies/edep-sim -B /tmp/edep-build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="${EDEPSIM_ROOT}" \
        -DEDEPSIM_DISPLAY=OFF \
    && cmake --build /tmp/edep-build --parallel "${BUILD_JOBS}" \
    && cmake --install /tmp/edep-build \
    && rm -rf /tmp/edep-build

RUN source dependencies/DLPGenerator/setup.sh \
    && make -C dependencies/DLPGenerator -j"${BUILD_JOBS}" \
    && chmod +x dependencies/DLPGenerator/bin/dlpgen \
    && test -x dependencies/DLPGenerator/bin/dlpgen

RUN python3 -m pip install --no-cache-dir \
        "scikit-build<0.19" \
        "setuptools>=69,<81" \
        wheel \
    && python3 -m pip install --no-cache-dir --no-build-isolation \
        ./dependencies/SuperaAtomic \
    && source /opt/geant4/bin/geant4.sh \
    && CMAKE_PREFIX_PATH="${EDEPSIM_ROOT}:${CMAKE_PREFIX_PATH:-}" \
       python3 -m pip install --no-cache-dir --no-build-isolation \
        ./dependencies/edep2supera

# Keep NEUT's ROOT 6.34 dependency private. This copy deliberately follows all
# expensive source builds so adding or updating NEUT does not invalidate them.
# dlpgen-opt-neut constructs a subprocess-only LD_LIBRARY_PATH for these files;
# global ROOT remains 6.32.02.
COPY --from=neut_upstream /opt/neut-export /opt/neut-runtime
COPY --from=neut_upstream /opt/neut-root-share /usr/share/root
COPY --from=neut_upstream /opt/neut-root-include /usr/include/root
COPY --from=neut_upstream /opt/neut-source/neutclass /opt/neut/5.8.0/src/neutclass
COPY dependencies/versions.yaml /opt/dlpgen-opt/dependencies/versions.yaml

COPY pyproject.toml README.md /opt/dlpgen-opt/
COPY src /opt/dlpgen-opt/src
COPY configs/slurm /opt/dlpgen-opt/configs/slurm
COPY docker/entrypoint.sh /usr/local/bin/dlpgen-opt-entrypoint

ENV ROOTEGPythia6_ROOT=/opt/rootegpythia6 \
    PYTHIA6=/opt/rootegpythia6/lib \
    NUWRO=/opt/nuwro \
    PATH="/opt/nuwro/bin:/opt/gibuu:${DLPGENERATOR_BINDIR}:${EDEPSIM_ROOT}/bin:${GENIE}/bin:${PYTHIA8}/bin:${PATH}" \
    LD_LIBRARY_PATH="/opt/nuwro/lib:/opt/rootegpythia6/lib:${DLPGENERATOR_LIBDIR}:${EDEPSIM_ROOT}/lib:${GENIE}/lib:${PYTHIA8}/lib:/opt/dk2nu/lib:${LD_LIBRARY_PATH}" \
    PYTHONPATH="${DLPGENERATOR_DIR}/python:${PYTHONPATH}" \
    DLPGEN_OPT_ROOT="/opt/dlpgen-opt" \
    GENIE_XSEC_FILE="/opt/genie/xsec/gxspl-AR23_20i_00_000.xml" \
    ROOT_INCLUDE_PATH="${GENIE}/include/GENIE:${DLPGENERATOR_INCDIR}/DLPGenerator/ParticleBomb:/opt/dk2nu/include:/usr/local/include/supera:/usr/local/include/supera/base:/usr/local/include/supera/data:/usr/local/include/supera/algorithm:/usr/local/include/supera/process:/usr/local/include/edep2supera"

RUN python3 -m pip install --no-cache-dir --no-build-isolation /opt/dlpgen-opt \
    && chmod +x /usr/local/bin/dlpgen-opt-entrypoint \
    && dlpgen-opt --version \
    && source /opt/geant4/bin/geant4.sh \
    && LD_LIBRARY_PATH="${EDEPSIM_ROOT}/lib:${LD_LIBRARY_PATH}" \
       ldd "${EDEPSIM_ROOT}/bin/edep-sim" \
       | awk '/not found/ { missing = 1 } END { exit missing }' \
    && python3 -c "import ROOT, larcv, pyhepmc, supera, edep2supera; print('runtime imports OK')" \
    && test "$(root-config --version)" = "6.32.02" \
    && test -x "${GENIE}/bin/gevgen_fnal" \
    && test -d "${GENIE}/config/hadronization/pythia6" \
    && test -d "${GENIE}/config/hadronization/pythia8" \
    && grep -q '^#define __GENIE_PYTHIA6_ENABLED__$' \
        "${GENIE}/src/Framework/Conventions/GBuild.h" \
    && grep -q '^#define __GENIE_PYTHIA8_ENABLED__$' \
        "${GENIE}/src/Framework/Conventions/GBuild.h" \
    && nm -D "${GENIE}/lib/libGPhDcy.so" | grep TPythia6 >/dev/null \
    && nm -D "${GENIE}/lib/libGPhHadnz.so" | grep Pythia6Hadro2019 >/dev/null \
    && nm -D "${GENIE}/lib/libGPhHadnz.so" | grep Pythia8Hadro2019 >/dev/null \
    && test -x /opt/gibuu/GiBUU.x \
    && test -x /opt/nuwro/bin/nuwro \
    && test -x /opt/nuwro/bin/nuwro2rootracker \
    && test -s /opt/gibuu/version.txt \
    && test -s /usr/share/source/gibuu/release${GIBUU_RELEASE}.tar.gz \
    && python3 -m dlpgen_opt.nuhepmc_cli --help >/dev/null \
    && python3 -m dlpgen_opt.flux_cli --help >/dev/null \
    && python3 -m dlpgen_opt.gibuu_cli --help >/dev/null \
    && python3 -m dlpgen_opt.nuwro_cli --help >/dev/null \
    && python3 -m dlpgen_opt.neut_cli --help >/dev/null \
    && test -x /opt/neut-runtime/neut/bin/neutroot2 \
    && test -x /opt/neut-runtime/neut/bin/neutvect-converter \
    && env -i PATH=/usr/bin:/bin \
       LD_LIBRARY_PATH=/opt/neut-runtime/neut/lib:/opt/neut-runtime/root:/opt/neut-runtime/nuhepmc/lib:/opt/neut-runtime/hepmc/lib64:/opt/neut-runtime/buildbox/lib64:/opt/neut-runtime/system \
       ldd /opt/neut-runtime/neut/bin/neutroot2 \
       | awk '/not found/ { missing = 1 } END { exit missing }' \
    && env -i PATH=/usr/bin:/bin \
       LD_LIBRARY_PATH=/opt/neut-runtime/neut/lib:/opt/neut-runtime/root:/opt/neut-runtime/nuhepmc/lib:/opt/neut-runtime/hepmc/lib64:/opt/neut-runtime/buildbox/lib64:/opt/neut-runtime/system \
       ldd /opt/neut-runtime/neut/bin/neutvect-converter \
       | awk '/not found/ { missing = 1 } END { exit missing }' \
    && test -s /opt/genie/xsec/gxspl-AR23_20i_00_000.xml \
    && test -s /opt/genie/xsec/gxspl-G18_10a_02_11b.xml \
    && test -s /opt/genie/xsec/gxspl-G18_10b_02_11b.xml \
    && test -s /opt/genie/xsec/gxspl-N24_20i_02_11b.xml \
    && ldd /opt/dk2nu/lib/libdk2nuGenie.so \
       | awk '/not found/ { missing = 1 } END { exit missing }'

WORKDIR /work
ENTRYPOINT ["/usr/local/bin/dlpgen-opt-entrypoint"]
CMD ["--help"]
