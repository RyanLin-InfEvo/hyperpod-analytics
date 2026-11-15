// SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
//
// SPDX-License-Identifier: AGPL-3.0-or-later

import 'package:flutter/material.dart';
import 'package:hyperpod_app/domain/domain.dart';

/// Root MaterialApp shell for HyperPod Analytics.
class HyperPodApp extends StatelessWidget {
  /// Creates the root [HyperPodApp] widget.
  const HyperPodApp({super.key});

  /// Required footer disclaimer per specification §8.2.
  static const String disclaimerText =
      'Not financial advice. Data from public Hyperliquid APIs.';

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'HyperPod',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF0E7490),
          brightness: Brightness.dark,
        ),
        fontFamily: 'Roboto',
        useMaterial3: true,
      ),
      home: const _ShellScaffold(),
    );
  }
}

class _ShellScaffold extends StatelessWidget {
  const _ShellScaffold();

  @override
  Widget build(BuildContext context) {
    final textTheme = Theme.of(context).textTheme;
    return Scaffold(
      appBar: AppBar(
        title: const Text('HyperPod'),
      ),
      body: Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                'HyperPod Analytics',
                style: textTheme.headlineSmall,
              ),
              const SizedBox(height: 8),
              Text(
                'Contract schema v$supportedSchemaVersion · '
                'Main perp DEX only',
                style: textTheme.bodyMedium,
              ),
            ],
          ),
        ),
      ),
      bottomNavigationBar: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        child: Text(
          HyperPodApp.disclaimerText,
          textAlign: TextAlign.center,
          style: textTheme.bodySmall,
        ),
      ),
    );
  }
}
