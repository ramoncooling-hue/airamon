'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
const {NtExecutable, NtExecutableResource, Data, Resource} = require('resedit');

// Resource editing in JavaScript keeps cross-builds independent of Wine.
module.exports = async ({appOutDir, packager}) => {
  const file=path.join(appOutDir,'MinDock.exe');
  const executable=NtExecutable.from(await fs.readFile(file));
  const resource=NtExecutableResource.from(executable);
  const icon=Data.IconFile.from(await fs.readFile(path.resolve(__dirname,'../../output/windows/build-resources/MinDock.ico')));
  const groups=Resource.IconGroupEntry.fromEntries(resource.entries);
  for(const group of groups) Resource.IconGroupEntry.replaceIconsForResource(resource.entries,group.id,group.lang,icon.icons.map(entry=>entry.data));
  for(const version of Resource.VersionInfo.fromEntries(resource.entries)) {
    version.setFileVersion(packager.appInfo.version);
    version.setProductVersion(packager.appInfo.version);
    for(const language of version.getAllLanguagesForStringValues()) version.setStringValues(language,{
      ProductName:'MinDock',FileDescription:'MinDock personal knowledge assistant',
      OriginalFilename:'MinDock.exe',InternalName:'MinDock',CompanyName:'MinDock',
      LegalCopyright:'MinDock'});
    version.outputToResourceEntries(resource.entries);
  }
  resource.outputResource(executable);
  await fs.writeFile(file,Buffer.from(executable.generate()));
};
