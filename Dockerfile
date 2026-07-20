# LArCV2 supplies the ROOT/PyROOT and output-I/O layer required by edep2supera.
# The tag is immutable at the release level; production deployments should also
# pin the resolved base-image digest in their build metadata.
FROM ghcr.io/deeplearnphysics/larcv2:2.4.1-ubuntu22.04@sha256:2a685aa58041e0fe81a4d23d119cda52a7c39db70709ce0871039fe35af0f6f8

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG DEBIAN_FRONTEND=noninteractive
ARG GEANT4_VERSION=11.4.2
ARG BUILD_JOBS=2

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
        libxerces-c-dev \
        make \
        ninja-build \
        python3-dev \
        python3-pip \
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

WORKDIR /opt/dlpgen-opt
COPY dependencies /opt/dlpgen-opt/dependencies

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
COPY docker/entrypoint.sh /usr/local/bin/dlpgen-opt-entrypoint

ENV PATH="${DLPGENERATOR_BINDIR}:${EDEPSIM_ROOT}/bin:${PATH}" \
    LD_LIBRARY_PATH="${DLPGENERATOR_LIBDIR}:${EDEPSIM_ROOT}/lib:${LD_LIBRARY_PATH}" \
    PYTHONPATH="${DLPGENERATOR_DIR}/python:${PYTHONPATH}" \
    DLPGEN_OPT_ROOT="/opt/dlpgen-opt" \
    ROOT_INCLUDE_PATH="${DLPGENERATOR_INCDIR}/DLPGenerator/ParticleBomb:/usr/local/include/supera:/usr/local/include/supera/base:/usr/local/include/supera/data:/usr/local/include/supera/algorithm:/usr/local/include/supera/process:/usr/local/include/edep2supera"

RUN python3 -m pip install --no-cache-dir --no-build-isolation /opt/dlpgen-opt \
    && chmod +x /usr/local/bin/dlpgen-opt-entrypoint \
    && dlpgen-opt --version \
    && source /opt/geant4/bin/geant4.sh \
    && LD_LIBRARY_PATH="${EDEPSIM_ROOT}/lib:${LD_LIBRARY_PATH}" \
       ldd "${EDEPSIM_ROOT}/bin/edep-sim" \
       | awk '/not found/ { missing = 1 } END { exit missing }' \
    && python3 -c "import ROOT, larcv, supera, edep2supera; print('runtime imports OK')"

WORKDIR /work
ENTRYPOINT ["/usr/local/bin/dlpgen-opt-entrypoint"]
CMD ["--help"]
