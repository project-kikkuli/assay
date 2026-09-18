import { writeFileSync } from "node:fs";

export default class TimingReporter {
  onTestRunEnd(testModules) {
    const modules = testModules.map((module) => {
      const diagnostic = module.diagnostic();
      const task = module.task ?? module;
      return {
        path: task.filepath,
        environment_setup_ms: diagnostic.environmentSetupDuration,
        prepare_ms: diagnostic.prepareDuration,
        transform_prepare_ms: diagnostic.prepareDuration,
        collect_ms: diagnostic.collectDuration,
        setup_ms: diagnostic.setupDuration,
        tests_and_hooks_ms: diagnostic.duration,
        import_durations_ms: Object.fromEntries(
          Object.entries(diagnostic.importDurations ?? {}).map(([key, value]) => [
            key,
            value.duration,
          ]),
        ),
      };
    });
    writeFileSync(process.env.VITEST_TIMING_FILE, JSON.stringify({ modules }));
  }
}
