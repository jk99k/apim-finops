---
name: eli5
description: Explain a topic like I'm a 5 year old. Use when the user types /eli5 <topic> or asks for a dead-simple picture explainer of how something works.
license: MIT
metadata:
  source: https://github.com/anthropics/claude-plugins-community/tree/main/eli5
  upstreamAuthor: Thariq Shihipar
  upstreamRepoLicense: Apache-2.0 (https://github.com/anthropics/claude-plugins-community/blob/main/LICENSE)
  upstreamPluginLicense: MIT (declared in eli5/.claude-plugin/plugin.json)
  modifications: Converted from the Claude plugin layout to the Copilot CLI skill format. The instruction text is unchanged from the original.
---

<!--
  Third-party content.

  Copyright (c) Thariq Shihipar
  Originally published in https://github.com/anthropics/claude-plugins-community
  under the eli5 plugin, which declares the MIT License. The upstream repository
  itself is licensed under Apache License 2.0.

  This file was modified: it was converted from the Claude plugin layout to the
  Copilot CLI skill format. The instruction text below is unchanged.

  This file is NOT covered by the repository-root LICENSE.
-->

# eli5

Explain like I'm someone who knows nothing about this topic, using a HTML artifact with big pictures and few words.

Topic: $ARGUMENTS
