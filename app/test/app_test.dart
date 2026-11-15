// SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
//
// SPDX-License-Identifier: AGPL-3.0-or-later

import 'dart:io';

import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:hyperpod_app/app/app.dart';
import 'package:hyperpod_app/domain/domain.dart';

Future<void> _loadBundledFonts() async {
  final robotoLoader = FontLoader('Roboto')
    ..addFont(
      Future<ByteData>.value(
        ByteData.sublistView(
          File('test/fonts/Roboto-Regular.ttf').readAsBytesSync(),
        ),
      ),
    )
    ..addFont(
      Future<ByteData>.value(
        ByteData.sublistView(
          File('test/fonts/Roboto-Medium.ttf').readAsBytesSync(),
        ),
      ),
    );
  await robotoLoader.load();

  final iconsLoader = FontLoader('MaterialIcons')
    ..addFont(
      Future<ByteData>.value(
        ByteData.sublistView(
          File('test/fonts/MaterialIcons-Regular.ttf').readAsBytesSync(),
        ),
      ),
    );
  await iconsLoader.load();
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUpAll(_loadBundledFonts);

  test('domain module exports expected schema contract version', () {
    expect(supportedSchemaVersion, equals(1));
  });

  testWidgets('renders HyperPod shell and required disclaimer', (tester) async {
    await tester.pumpWidget(const ProviderScope(child: HyperPodApp()));

    expect(find.text('HyperPod'), findsOneWidget);
    expect(find.text('HyperPod Analytics'), findsOneWidget);
    expect(find.text(HyperPodApp.disclaimerText), findsOneWidget);
  });
}
