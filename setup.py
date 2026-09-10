"""Build only the reviewed operational closure; research stays source-only."""
from pathlib import Path
import tomllib

from setuptools import setup
from setuptools.command.build_py import build_py


class OperationalBuild(build_py):
    def run(self):
        super().run()
        config=tomllib.loads((Path(__file__).parent/'pyproject.toml').read_text(encoding='utf-8'))
        expected={m.replace('.','/')+'.py' for m in config['tool']['stock_data']['release']['modules']}
        actual={p.relative_to(self.build_lib).as_posix() for p in Path(self.build_lib).rglob('*.py')}
        if actual != expected:
            raise RuntimeError('dirty or incomplete release closure; use a new build directory: '+str(sorted(actual^expected)))

    def find_package_modules(self, package, package_dir):
        config=tomllib.loads((Path(__file__).parent/'pyproject.toml').read_text(encoding='utf-8'))
        allowed=set(config['tool']['stock_data']['release']['modules'])
        return [item for item in super().find_package_modules(package,package_dir)
                if item[0]+'.'+item[1] in allowed]


setup(cmdclass={'build_py':OperationalBuild})
