import datetime
import sys

import tomlkit
import os
from typing import List
import git


class Config:
    file: str
    toml: tomlkit.TOMLDocument = None
    _instance = None
    _initialized: bool = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Config, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self.file = os.path.join(os.path.dirname(__file__), 'spec.toml')
        self.toml = tomlkit.TOMLDocument()
        self.reload()
        self._initialized = True

    def reload(self):
        self.toml.clear()
        with open(self.file, 'r') as f:
            self.toml = tomlkit.load(f)

    def save(self):
        self.toml['global']['saved_at'] = datetime.datetime.now()
        with open(self.file, 'w') as f:
            tomlkit.dump(self.toml, f)

        repo_path = os.path.dirname(os.path.dirname(self.file))
        file_path = self.file.removeprefix(repo_path + os.path.sep)
        repo = git.Repo(repo_path)
        if file_path in repo.git.diff(None, name_only=True):
            try:
                repo.git.add(file_path)
                repo.index.commit('Saved changes')
                origin = repo.remotes['origin']
                origin.push(str(repo.active_branch))
            except Exception as e:
                print(f"Exception: {e}")


class DeepSearchResult:

    def __init__(self, path: str, value):
        self.path = path
        self.value = value


def deep_search(d:dict, what:str, path: str = None, found: list = None) -> List[DeepSearchResult]:
    """
    Performs a deep search of a keyword in a dictionary
    :param d: The dictionary to be searched
    :param what: The keyword to search for
    :param path:
    :param found:
    :return:
    """

    if found is None:
        found = list()

    for key, value in d.items():
        if isinstance(d[key], dict):
            deep_search(d[key], what, key if path is None else path + '.' + key, found)
        else:
            if key == what:
                f = DeepSearchResult(key if path is None else path + '.' + key, value)
                found.append(f)
                return found
    return found


if __name__ == '__main__':
    results = deep_search(Config().toml, 'address')
    for result in results:
        print(f"{result.path=}, {result.value=}")
