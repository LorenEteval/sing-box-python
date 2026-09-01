import json
import os
import pathlib
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys

from setuptools import Extension, find_packages, setup
from setuptools.command.build_ext import build_ext


ROOT_DIR = pathlib.Path(__file__).parent.resolve()
GO_SOURCE_DIR = ROOT_DIR / 'singbox-go'
UPSTREAM_VERSION = (ROOT_DIR / 'UPSTREAM_VERSION').read_text(encoding='utf-8').strip()

if not re.fullmatch(r'v\d+\.\d+\.\d+', UPSTREAM_VERSION):
    raise RuntimeError(f'Invalid upstream version: {UPSTREAM_VERSION!r}')

PACKAGE_VERSION = UPSTREAM_VERSION[1:]


class CMakeExtension(Extension):
    '''A setuptools extension whose sources are produced by CMake.'''

    def __init__(self, name):
        super().__init__(name, sources=[])


class BuildSingBox(build_ext):
    '''Build sing-box as a Go c-archive, then link the Python extension.'''

    CMAKE_GROUPED_LINK_OPTIONS = frozenset(
        {
            '-framework',
            '-lazy_framework',
            '-reexport_framework',
            '-weak_framework',
        }
    )

    def build_extension(self, ext):
        if self.dry_run:
            return

        extension_path = pathlib.Path(self.get_ext_fullpath(ext.name)).resolve()
        build_dir = pathlib.Path(self.build_temp).resolve() / ext.name
        go_output_dir = build_dir / 'go'
        cmake_build_dir = build_dir / 'cmake'
        native_output_dir = build_dir / 'native'

        for directory in (go_output_dir, cmake_build_dir, native_output_dir):
            directory.mkdir(parents=True, exist_ok=True)

        archive_name = 'singbox.lib' if platform.system() == 'Windows' else 'singbox.a'
        archive_path = go_output_dir / archive_name
        cgo_link_items_path = go_output_dir / 'cgo-link-items.cmake'

        env = os.environ.copy()
        env['CGO_ENABLED'] = '1'
        env['GOTOOLCHAIN'] = 'local'
        env['GOCACHE'] = str(ROOT_DIR / 'build' / 'go-cache')

        macos_architecture = None

        if platform.system() == 'Darwin':
            macos_architecture = self.macos_architecture(env)

            if macos_architecture:
                try:
                    env['GOARCH'] = {'arm64': 'arm64', 'x86_64': 'amd64'}[
                        macos_architecture
                    ]
                except KeyError as error:
                    raise RuntimeError(
                        f'Unsupported macOS architecture: {macos_architecture}'
                    ) from error

                env['GOOS'] = 'darwin'

        tags = os.environ.get('SINGBOX_BUILD_TAGS', self.default_build_tags())
        tag_set = set(tags.split(','))
        cgo_link_items = self.cgo_link_items(env, tags)
        self.write_cmake_link_items(cgo_link_items_path, cgo_link_items)
        self.announce(
            f'Discovered {len(cgo_link_items)} cgo linker items',
            level=2,
        )
        ldflags = ' '.join(
            (
                f'-X github.com/sagernet/sing-box/constant.Version={UPSTREAM_VERSION}',
                (GO_SOURCE_DIR / 'release' / 'LDFLAGS').read_text(
                    encoding='utf-8'
                ).strip(),
                '-s -w -buildid=',
            )
        )

        self.run_command(
            [
                'go',
                'build',
                '-o',
                str(archive_path),
                '-buildmode=c-archive',
                '-buildvcs=false',
                '-trimpath',
                '-tags',
                tags,
                '-ldflags',
                ldflags,
                './binding',
            ],
            cwd=GO_SOURCE_DIR,
            env=env,
        )

        cronet_artifact = self.cronet_artifact(env, tag_set)
        downloaded_shared_cronet = (
            cronet_artifact
            if cronet_artifact is not None and cronet_artifact.suffix != '.a'
            else None
        )

        cmake_args = [
            'cmake',
            '-S',
            str(ROOT_DIR),
            '-B',
            str(cmake_build_dir),
            '-DCMAKE_BUILD_TYPE=Release',
            f'-DCMAKE_LIBRARY_OUTPUT_DIRECTORY={native_output_dir.as_posix()}',
            f'-DCMAKE_RUNTIME_OUTPUT_DIRECTORY={native_output_dir.as_posix()}',
            f'-DCMAKE_LIBRARY_OUTPUT_DIRECTORY_RELEASE={native_output_dir.as_posix()}',
            f'-DCMAKE_RUNTIME_OUTPUT_DIRECTORY_RELEASE={native_output_dir.as_posix()}',
            f'-DPython_EXECUTABLE={pathlib.Path(sys.executable).as_posix()}',
            self.pybind11_cmake_argument(),
            f'-DSINGBOX_ARCHIVE={archive_path.as_posix()}',
            f'-DSINGBOX_INCLUDE_DIR={go_output_dir.as_posix()}',
            f'-DSINGBOX_CGO_LINK_ITEMS_FILE={cgo_link_items_path.as_posix()}',
        ]

        if cronet_artifact is not None and cronet_artifact.suffix == '.a':
            cmake_args.append(
                f'-DSINGBOX_CRONET_ARCHIVE={cronet_artifact.as_posix()}'
            )
        if cronet_artifact is not None:
            cmake_args.append('-DSINGBOX_CRONET_SHARED=ON')
        if macos_architecture:
            cmake_args.append(f'-DCMAKE_OSX_ARCHITECTURES={macos_architecture}')
        if platform.system() == 'Windows':
            cmake_args.extend(
                [
                    '-G',
                    'MinGW Makefiles',
                    '-DCMAKE_C_COMPILER=gcc',
                    '-DCMAKE_CXX_COMPILER=g++',
                ]
            )

        self.run_command(cmake_args, cwd=ROOT_DIR, env=env)
        self.run_command(
            [
                'cmake',
                '--build',
                str(cmake_build_dir),
                '--config',
                'Release',
                '--target',
                '_native',
                '--parallel',
            ],
            cwd=ROOT_DIR,
            env=env,
        )

        candidates = []

        for pattern in ('_native*.pyd', '_native*.so', '_native*.dylib'):
            candidates.extend(native_output_dir.rglob(pattern))

        if len(candidates) != 1:
            found = ', '.join(str(path) for path in candidates) or 'none'

            raise RuntimeError(f'Expected one native singbox extension, found: {found}')

        extension_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidates[0], extension_path)

        bundled_cronet = downloaded_shared_cronet
        if platform.system() == 'Darwin' and cronet_artifact is not None:
            bundled_cronet = native_output_dir / 'libcronet.dylib'

        if bundled_cronet is not None:
            if not bundled_cronet.is_file():
                raise RuntimeError(
                    f'Expected the bundled Cronet runtime at {bundled_cronet}'
                )
            self.copy_runtime_artifact(
                bundled_cronet,
                extension_path.parent / bundled_cronet.name,
            )

    @staticmethod
    def run_command(command, cwd, env):
        subprocess.run(command, cwd=str(cwd), env=env, check=True)

    @staticmethod
    def copy_runtime_artifact(source, destination):
        if destination.exists():
            destination.chmod(destination.stat().st_mode | stat.S_IWUSR)

        shutil.copyfile(source, destination)

    @staticmethod
    def pybind11_cmake_argument():
        import pybind11

        return f'-Dpybind11_DIR={pathlib.Path(pybind11.get_cmake_dir()).as_posix()}'

    @classmethod
    def cgo_link_items(cls, env, tags):
        result = subprocess.run(
            [
                'go',
                'list',
                '-deps',
                '-json',
                '-tags',
                tags,
                './binding',
            ],
            cwd=str(GO_SOURCE_DIR),
            env=env,
            check=True,
            stdout=subprocess.PIPE,
            encoding='utf-8',
        )
        decoder = json.JSONDecoder()
        offset = 0
        link_items = []

        while offset < len(result.stdout):
            while offset < len(result.stdout) and result.stdout[offset].isspace():
                offset += 1
            if offset == len(result.stdout):
                break

            package, offset = decoder.raw_decode(result.stdout, offset)
            package_name = package.get('ImportPath', '<unknown>')
            link_items.extend(
                cls.normalize_cgo_link_flags(
                    package.get('CgoLDFLAGS', ()),
                    package_name,
                )
            )

        return link_items

    @classmethod
    def normalize_cgo_link_flags(cls, flags, package_name):
        link_items = []
        index = 0

        while index < len(flags):
            flag = flags[index]

            if not isinstance(flag, str) or not flag:
                raise RuntimeError(
                    f'Invalid cgo linker flag from {package_name}: {flag!r}'
                )
            if any(character in flag for character in ('\0', '\r', '\n')):
                raise RuntimeError(
                    f'Invalid cgo linker flag from {package_name}: {flag!r}'
                )

            if flag in cls.CMAKE_GROUPED_LINK_OPTIONS:
                if index + 1 == len(flags):
                    raise RuntimeError(
                        f'cgo linker option {flag!r} from {package_name} '
                        'has no argument'
                    )
                argument = flags[index + 1]
                if not isinstance(argument, str) or not argument:
                    raise RuntimeError(
                        f'Invalid argument for cgo linker option {flag!r} '
                        f'from {package_name}: {argument!r}'
                    )
                if any(character in argument for character in ('\0', '\r', '\n')):
                    raise RuntimeError(
                        f'Invalid argument for cgo linker option {flag!r} '
                        f'from {package_name}: {argument!r}'
                    )
                link_items.append(f'{flag} {argument}')
                index += 2
                continue

            link_items.append(flag)
            index += 1

        return link_items

    @classmethod
    def write_cmake_link_items(cls, destination, link_items):
        lines = [f'set(SINGBOX_CGO_LINK_ITEM_COUNT {len(link_items)})']

        for index, item in enumerate(link_items):
            lines.append(
                f'set(SINGBOX_CGO_LINK_ITEM_{index} '
                f'{cls.cmake_bracket_argument(item)})'
            )

        destination.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    @staticmethod
    def cmake_bracket_argument(value):
        equals = ''

        while f']{equals}]' in value:
            equals += '='

        return f'[{equals}[{value}]{equals}]'

    @staticmethod
    def default_build_tags():
        if platform.system() == 'Windows':
            source = GO_SOURCE_DIR / 'release' / 'DEFAULT_BUILD_TAGS_WINDOWS'
        else:
            source = GO_SOURCE_DIR / 'release' / 'DEFAULT_BUILD_TAGS'

        tags = source.read_text(encoding='utf-8').strip().split(',')

        if platform.system() in {'Darwin', 'Linux'} and 'with_purego' not in tags:
            tags.append('with_purego')
        if 'with_v2ray_api' not in tags:
            tags.append('with_v2ray_api')

        return ','.join(tags)

    @staticmethod
    def cronet_artifact(env, tags):
        if 'with_naive_outbound' not in tags:
            return None

        result = subprocess.run(
            ['go', 'env', 'GOOS', 'GOARCH'],
            cwd=str(GO_SOURCE_DIR),
            env=env,
            check=True,
            capture_output=True,
            encoding='utf-8',
        )
        target = result.stdout.splitlines()

        if len(target) != 2:
            raise RuntimeError(f'Unexpected output from go env: {result.stdout!r}')

        goos, goarch = target

        if goos not in {'darwin', 'linux', 'windows'} or goarch not in {
            'amd64',
            'arm64',
        }:
            raise RuntimeError(f'Cronet is unsupported for {goos}/{goarch}')

        module = f'github.com/sagernet/cronet-go/lib/{goos}_{goarch}'
        result = subprocess.run(
            ['go', 'mod', 'download', '-json', module],
            cwd=str(GO_SOURCE_DIR),
            env=env,
            check=True,
            capture_output=True,
            encoding='utf-8',
        )
        module_info = json.loads(result.stdout)
        module_dir = pathlib.Path(module_info['Dir'])

        if goos == 'darwin':
            if 'with_purego' not in tags:
                raise RuntimeError(
                    'macOS Naive builds require with_purego so Cronet can be '
                    'isolated from the Python extension C++ runtime'
                )

            artifact = module_dir / 'libcronet.a'
        elif goos == 'linux':
            if 'with_purego' not in tags:
                raise RuntimeError(
                    'Linux Naive builds require with_purego so libcronet.so can '
                    'be bundled in the wheel'
                )

            artifact = module_dir / 'libcronet.so'
        else:
            artifact = module_dir / 'libcronet.dll'

        if not artifact.is_file():
            raise RuntimeError(f'Cronet artifact is missing from {module}: {artifact}')

        return artifact

    @staticmethod
    def macos_architecture(env):
        flags = shlex.split(env.get('ARCHFLAGS', ''))
        architectures = []

        for index, flag in enumerate(flags):
            if flag != '-arch':
                continue
            if index + 1 == len(flags):
                raise RuntimeError('ARCHFLAGS ends with -arch but no architecture')

            architecture = flags[index + 1]

            if architecture not in architectures:
                architectures.append(architecture)

        if len(architectures) > 1:
            raise RuntimeError(
                'Universal2 is unsupported for Go c-archives; build separate '
                'arm64 and x86_64 wheels'
            )

        return architectures[0] if architectures else None


with (ROOT_DIR / 'README.md').open(encoding='utf-8') as readme:
    long_description = readme.read()


setup(
    name='sing-box-python',
    version=PACKAGE_VERSION,
    description='Python binding for sing-box',
    long_description=long_description,
    long_description_content_type='text/markdown',
    license='GPL-3.0-or-later',
    python_requires='>=3.8',
    packages=find_packages(),
    package_data={
        'singbox': [
            'py.typed',
            '_native.pyi',
            'libcronet.dll',
            'libcronet.dylib',
            'libcronet.so',
        ]
    },
    include_package_data=True,
    ext_modules=[CMakeExtension('singbox._native')],
    cmdclass={'build_ext': BuildSingBox},
    zip_safe=False,
    project_urls={
        'Source': 'https://github.com/LorenEteval/sing-box-python',
        'sing-box': 'https://github.com/SagerNet/sing-box',
    },
    classifiers=[
        'Development Status :: 4 - Beta',
        'Programming Language :: C++',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3 :: Only',
        'Operating System :: Microsoft :: Windows',
        'Operating System :: POSIX :: Linux',
        'Operating System :: MacOS :: MacOS X',
        'Topic :: Internet :: Proxy Servers',
    ],
)
