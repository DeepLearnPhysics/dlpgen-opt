# LArCV2 supplies the ROOT/PyROOT and output-I/O layer required by edep2supera.
# The tag is immutable at the release level; production deployments should also
# pin the resolved base-image digest in their build metadata.
FROM ghcr.io/deeplearnphysics/larcv2:2.4.1-ubuntu22.04@sha256:2a685aa58041e0fe81a4d23d119cda52a7c39db70709ce0871039fe35af0f6f8

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

# Keep configuration/compilation separate from installation. A successful
# compile is then an ordinary cached image layer, so a later installer or
# Docker-engine interruption does not force a full Geant4 rebuild.
RUN --mount=type=cache,id=dlpgen-opt-geant4-${GEANT4_VERSION},target=/tmp/geant4-build \
    cmake -S /opt/geant4-source -B /tmp/geant4-build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/opt/geant4 \
        -DGEANT4_BUILD_MULTITHREADED=ON \
        -DGEANT4_INSTALL_DATA=ON \
        -DGEANT4_USE_GDML=ON \
        -DGEANT4_USE_OPENGL_X11=OFF \
        -DGEANT4_USE_QT=OFF \
    && cmake --build /tmp/geant4-build --parallel "${BUILD_JOBS}"

RUN --mount=type=cache,id=dlpgen-opt-geant4-${GEANT4_VERSION},target=/tmp/geant4-build \
    cmake --install /tmp/geant4-build > /tmp/geant4-install.log \
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

# GENIE 3.6.2 supports Pythia8 without ROOT's optional TPythia6 interface.
# Build the official Pythia 8.317 release explicitly and pin the source archive
# checksum so the common image does not need the legacy Pythia6/ROOT adapter.
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

# Keep GENIE and dk2nu as pinned source submodules, but copy them into
# temporary build locations so their object files do not inflate the runtime
# dependency tree retained under /opt/dlpgen-opt.
COPY dependencies/GENIE /tmp/genie-pristine
COPY docker/patches/genie-dkmeta-output.patch /tmp/genie-dkmeta-output.patch
ENV GENIE=/opt/genie \
    PYTHIA8=/opt/pythia8 \
    PYTHIA8DATA=/opt/pythia8/share/Pythia8/xmldoc \
    LD_LIBRARY_PATH=/opt/pythia8/lib:${LD_LIBRARY_PATH}
# HEDIS is an ultra-high-energy module and unconditionally requires LHAPDF
# even in GENIE 3.6.2 builds configured with LHAPDF disabled. It is irrelevant
# to accelerator-beam energies, so omit it and the structure-function utilities
# that require LHAPDF rather than carrying LHAPDF and PDF datasets.
RUN --mount=type=cache,id=dlpgen-opt-genie-${GENIE_VERSION},target=/tmp/genie-source \
    rsync -a /tmp/genie-pristine/ /tmp/genie-source/ \
    && git -C /tmp apply --no-index --directory=genie-source \
        /tmp/genie-dkmeta-output.patch \
    && sed -i '/Physics\/HEDIS/d' /tmp/genie-source/Makefile \
    && sed -i '/^TGT_BASE =/,/^$/ { /gmkhedissf/d; /gcalchedisdiffxsec/d; /gmkphotonsf/d; }' \
        /tmp/genie-source/src/Apps/Makefile \
    && sed -i 's/ -lGPhHEDISXS -lGPhHEDISEG//' \
        /tmp/genie-source/src/scripts/setup/genie-config \
    && cd /tmp/genie-source \
    && GENIE=/tmp/genie-source ./configure \
        --prefix=/opt/genie \
        --disable-pythia6 \
        --enable-pythia8 \
        --with-pythia8-inc=/opt/pythia8/include \
        --with-pythia8-lib=/opt/pythia8/lib \
        --disable-lhapdf5 \
        --disable-lhapdf6 \
        --enable-flux-drivers \
        --enable-geom-drivers \
        --enable-fnal \
    && GENIE=/tmp/genie-source make -j"${BUILD_JOBS}"

RUN --mount=type=cache,id=dlpgen-opt-genie-${GENIE_VERSION},target=/tmp/genie-source \
    (GENIE=/tmp/genie-source make -C /tmp/genie-source install \
        > /tmp/genie-install.log 2>&1 \
        || { tail -n 120 /tmp/genie-install.log; exit 1; }) \
    && cp -a /tmp/genie-source/config /tmp/genie-source/data \
        /tmp/genie-source/VERSION /opt/genie/ \
    && test -x /opt/genie/bin/gevgen_fnal \
    && test -x /opt/genie/bin/gntpc

# GENIE 3.6.2's shared defaults still select Pythia6 implementations even when
# built with Pythia8. Select the corresponding Pythia8 decayer and hadronizers;
# this avoids rebuilding ROOT with the retired TPythia6 adapter.
RUN sed -i \
        -e 's/genie::Pythia6Decayer2023/genie::Pythia8Decayer2023/g' \
        -e 's/genie::Pythia6Hadro2019/genie::Pythia8Hadro2019/g' \
        -e 's/genie::AGCharmPythia6Hadro2023/genie::AGCharmPythia8Hadro2023/g' \
        /opt/genie/config/UnstableParticleDecayer.xml \
        /opt/genie/config/AGKYLowW2019.xml \
        /opt/genie/config/AGKY2019.xml \
        /opt/genie/config/DISHadronicSystemGenerator.xml \
    && ! grep -Eq 'genie::(Pythia6Decayer2023|Pythia6Hadro2019|AGCharmPythia6Hadro2023)' \
        /opt/genie/config/UnstableParticleDecayer.xml \
        /opt/genie/config/AGKYLowW2019.xml \
        /opt/genie/config/AGKY2019.xml \
        /opt/genie/config/DISHadronicSystemGenerator.xml

# ROOT's exported CMake configuration declares this dependency. The LArCV
# base contains the runtime header use, but dk2nu configuration also needs the
# package's CMake metadata.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nlohmann-json3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY dependencies/dk2nu /tmp/dk2nu-source
RUN --mount=type=cache,id=dlpgen-opt-genie-${GENIE_VERSION},target=/tmp/genie-source \
    --mount=type=cache,id=dlpgen-opt-dk2nu,target=/tmp/dk2nu-build \
    GENIE=/tmp/genie-source \
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
    && GENIE=/tmp/genie-source \
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

WORKDIR /opt/dlpgen-opt
COPY dependencies/DLPGenerator /opt/dlpgen-opt/dependencies/DLPGenerator
COPY dependencies/edep-sim /opt/dlpgen-opt/dependencies/edep-sim
COPY dependencies/SuperaAtomic /opt/dlpgen-opt/dependencies/SuperaAtomic
COPY dependencies/edep2supera /opt/dlpgen-opt/dependencies/edep2supera
COPY dependencies/versions.yaml /opt/dlpgen-opt/dependencies/versions.yaml

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

COPY pyproject.toml README.md /opt/dlpgen-opt/
COPY src /opt/dlpgen-opt/src
COPY configs/slurm /opt/dlpgen-opt/configs/slurm
COPY docker/entrypoint.sh /usr/local/bin/dlpgen-opt-entrypoint

ENV PATH="/opt/gibuu:${DLPGENERATOR_BINDIR}:${EDEPSIM_ROOT}/bin:${GENIE}/bin:${PYTHIA8}/bin:${PATH}" \
    LD_LIBRARY_PATH="${DLPGENERATOR_LIBDIR}:${EDEPSIM_ROOT}/lib:${GENIE}/lib:${PYTHIA8}/lib:/opt/dk2nu/lib:${LD_LIBRARY_PATH}" \
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
    && test -x /opt/gibuu/GiBUU.x \
    && test -s /opt/gibuu/version.txt \
    && test -s /usr/share/source/gibuu/release${GIBUU_RELEASE}.tar.gz \
    && python3 -m dlpgen_opt.nuhepmc_cli --help >/dev/null \
    && python3 -m dlpgen_opt.flux_cli --help >/dev/null \
    && python3 -m dlpgen_opt.gibuu_cli --help >/dev/null \
    && test -s /opt/genie/xsec/gxspl-AR23_20i_00_000.xml \
    && test -s /opt/genie/xsec/gxspl-G18_10a_02_11b.xml \
    && test -s /opt/genie/xsec/gxspl-G18_10b_02_11b.xml \
    && test -s /opt/genie/xsec/gxspl-N24_20i_02_11b.xml \
    && ldd /opt/dk2nu/lib/libdk2nuGenie.so \
       | awk '/not found/ { missing = 1 } END { exit missing }'

WORKDIR /work
ENTRYPOINT ["/usr/local/bin/dlpgen-opt-entrypoint"]
CMD ["--help"]
