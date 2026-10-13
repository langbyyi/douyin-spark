'use strict';

// Managed task trees must remain in their supervisor's POSIX process group.
// Playwright otherwise starts browser processes with detached:true.
const childProcess = require('node:child_process');
const originalSpawn = childProcess.spawn;
const originalKill = process.kill;
const managedChildren = new Map();

childProcess.spawn = function (...args) {
  const optionsIndex = Array.isArray(args[1]) ? 2 : 1;
  const options = args[optionsIndex];
  const wasDetached = options && options.detached === true;
  if (wasDetached) {
    args[optionsIndex] = { ...options, detached: false };
  }
  const child = Reflect.apply(originalSpawn, this, args);
  if (wasDetached && Number.isInteger(child.pid) && child.pid > 0
      && child.exitCode === null && child.signalCode === null) {
    managedChildren.set(child.pid, child);
    child.once('exit', () => {
      if (managedChildren.get(child.pid) === child) {
        managedChildren.delete(child.pid);
      }
    });
  }
  return child;
};

// Playwright force-close sends to -browserPid, expecting a detached group.
// Forward only our live, non-detached direct children; the task scope owns
// their descendants and completes tree cleanup when the command finishes.
process.kill = function (...args) {
  const child = typeof args[0] === 'number' && args[0] < 0
    ? managedChildren.get(-args[0]) : undefined;
  if (child) {
    if (child.exitCode === null && child.signalCode === null) {
      args[0] = child.pid;
    } else {
      managedChildren.delete(child.pid);
    }
  }
  return Reflect.apply(originalKill, this, args);
};

require('node:module').syncBuiltinESMExports();
