//=============================================================================
// SteamB23_HangulNameEdit.js
//=============================================================================

/*:
 * @plugindesc 한글 이름 입력창 2.1v
 * @author SteamB23
 * 
 * @param 자판 형식
 * @desc 초기 자판 형식
 * 0 - 기본, 1 - 창제
 * @default 0
 * 
 * 
 * @help
 * ==============================================================================
 * 개요
 * ==============================================================================
 * 
 * 이 플러그인은 기본 이름 입력창을 개조 및 확장하여 조합형 방식으로 한글
 * 입력을 가능하게 만들어 줍니다.
 * 
 * ==============================================================================
 * 사양
 * ==============================================================================
 * 
 * - 한글 자판과 영문/특수 문자 자판 페이지로 구분되어있습니다.
 * - 한글 자판은 자음 키, 모음 키, 특수 키, 빈 키로 구성되어있습니다.
 * - 자음 키는 초성과 종성을 입력할때 사용됩니다.
 * - 모음 키는 중성을 입력할때 사용됩니다.
 * - 빈 키는 띄어쓰기가 할당되어 있고, 특수 키는 '영/특', '확인'키를 뜻합니다.
 * - 한글 입력 상태는 반드시 초성 - 중성 - 종성 순으로 바뀌며, 이 순서가
 *   틀렸거나 다른 문자가 오면 상태가 초기화 됩니다.
 * - 두벌식과 유사하게 도깨비불 현상이 발생합니다.
 * - 현재 입력 중인 한글에서 백스페이스키를 누른 듯, 낱자만 지울 수 있습니다.
 * 
 * ==============================================================================
 * 플러그인 명령
 * ==============================================================================
 * 
 * 플러그인 명령을 통해 자판 설정을 변경 할 수 있습니다.
 * 예)
 * HangulType 0
 * HangulType default
 * 한글자판 0
 * 한글자판 기본
 * ...
 * 
 * 설정값은 아래에 자판 설명을 확인하세요. (한글 명칭, 영문 명칭, 번호)
 * 
 * ==============================================================================
 * 한글 자판 - 기본 자판 : 기본, default, 0
 * ==============================================================================
 * 
 * 현대 한글 낱자 순서대로 정렬된 자판입니다. 겹받침은 따로 배치되어있습니다.
 * ㄱ ㄲ ㄴ ㄷ ㄸ  ㅏ ㅐ ㅑ ㅒ ㅓ
 * ㄹ ㅁ ㅂ ㅃ ㅅ  ㅔ ㅕ ㅖ ㅗ ㅘ
 * ㅆ ㅇ ㅈ ㅉ ㅊ  ㅙ ㅚ ㅛ ㅜ ㅝ
 * ㅋ ㅌ ㅍ ㅎ     ㅞ ㅟ ㅠ ㅡ ㅢ
 * ㄳ ㄵ ㄶ ㄺ ㄻ  ㅣ
 * ㄼ ㄽ ㄾ ㄿ ㅀ  ㅄ
 * 
 * ==============================================================================
 * 한글 자판 - 창제 자판 : 창제, changje, 1
 * ==============================================================================
 * 
 * 한글 창제 원리에 따라 정렬된 자판입니다.
 * ㄱ ㄴ ㅁ ㅅ ㅇ  ㅏ ㅓ ㅗ ㅜ ㅡ
 * ㄲ ㄷ ㅂ ㅆ ㅎ  ㅑ ㅕ ㅛ ㅠ ㅣ
 * ㅋ ㄸ ㅃ ㅈ     ㅐ ㅔ ㅘ ㅝ ㅢ
 *    ㅌ ㅍ ㅉ     ㅒ ㅖ ㅙ ㅞ
 *    ㄹ ㅊ           ㅚ ㅟ
 * ㄳ ㄵ ㄶ ㄺ ㄻ  ㄼ ㄽ ㄾ ㄿ ㅀ
 *                             ㅄ
 * ==============================================================================
 * 주의점
 * ==============================================================================
 * 
 * 마우스 사용시 RPG Maker MV 시스템상의 한계로 글자를 입력할 때 더블클릭 해야
 * 합니다. 다만 '국기'같은 단어에서 종성과 다음 글자의 초성이 같을 때에는
 * 더블클릭하여 종성을 입력한후 한번 클릭하여 초성을 입력하셔야 합니다.
 * 예)
 * ㄱㄱ ㅜㅜ ㄱㄱ ㄱㄱ ㅣㅣ = X
 * ㄱㄱ ㅜㅜ ㄱㄱ ㄱ ㅣㅣ = O
 * 
 * ==============================================================================
 * 라이센스
 * ==============================================================================
 * 
 * 이 플러그인은 MIT License로 배포됩니다.
 * 
 * ==============================================================================
 * MIT License 전문
 * ==============================================================================
 * 
 * The MIT License (MIT)
 * 
 * Copyright (c) 2016 SteamB23
 * 
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 * 
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 * 
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 * 
 * ==============================================================================
 * 업데이트 기록
 * ==============================================================================
 * 2016-02-13   2.1v
 * - 창제 자판에서 ㄹ이 누락된 것을 수정했습니다.
 
 * 2016-02-13   2.0v
 * - 기존 한글 자판 배열이 이상하게 되어있던 것을 수정했습니다.
 * - 한글 자판 설정 기능을 추가했습니다.
 * - 창제 자판을 추가했습니다.
 * - 낱자 지우기 기능을 추가했습니다.
 * - 일부 코드를 정리했습니다.
 * 
 * 2016-02-12   1.0v
 * - 첫 배포
 */


(function () {
    var parameters = PluginManager.parameters('SteamB23_HangulNameEdit');
    var hangulType = Number(parameters['HangulType'] || '0');

    var _Game_Interpreter_pluginCommand =
            Game_Interpreter.prototype.pluginCommand;
    Game_Interpreter.prototype.pluginCommand = function (command, args) {
        _Game_Interpreter_pluginCommand.call(this, command, args);
        if (command === 'HangulType' || command === '한글자판') {
            switch (args[0]) {
                case '기본':
                case 'default':
                case '0':
                    hangulType = 0;
                    break;
                case '창제':
                case 'changje':
                case '1':
                    hangulType = 1;
                    break;
            }
        }
    };

    //===============================================================================
    // Window_NameInput
    //===============================================================================
    Window_NameInput.HANGUL1 =
        [ 'ㄱ','ㄲ','ㄴ','ㄷ','ㄸ',  'ㅏ','ㅐ','ㅑ','ㅒ','ㅓ',
          'ㄹ','ㅁ','ㅂ','ㅃ','ㅅ',  'ㅔ','ㅕ','ㅖ','ㅗ','ㅘ',
          'ㅆ','ㅇ','ㅈ','ㅉ','ㅊ',  'ㅙ','ㅚ','ㅛ','ㅜ','ㅝ',
          'ㅋ','ㅌ','ㅍ','ㅎ',' ',   'ㅞ','ㅟ','ㅠ','ㅡ','ㅢ',
          'ㄳ','ㄵ','ㄶ','ㄺ','ㄻ',  'ㅣ',' ',' ',' ',' ',
          'ㄼ','ㄽ','ㄾ','ㄿ','ㅀ',  'ㅄ',' ',' ',' ',' ',
          ' ',' ',' ',' ',' ',       ' ',' ',' ',' ',' ',
          ' ',' ',' ',' ',' ',       ' ',' ',' ',' ',' ',
          ' ',' ',' ',' ',' ',       ' ',' ',' ','영/특','확인'];
    Window_NameInput.HANGUL2 =
        [ 'ㄱ','ㄴ','ㅁ','ㅅ','ㅇ',  'ㅏ','ㅓ','ㅗ','ㅜ','ㅡ',
          'ㄲ','ㄷ','ㅂ','ㅆ','ㅎ',  'ㅑ','ㅕ','ㅛ','ㅠ','ㅣ',
          'ㅋ','ㄸ','ㅃ','ㅈ',' ',   'ㅐ','ㅔ','ㅘ','ㅝ','ㅢ',
          ' ', 'ㅌ','ㅍ','ㅉ',' ',   'ㅒ','ㅖ','ㅙ','ㅞ',' ',
          ' ', 'ㄹ',' ', 'ㅊ',' ',   ' ', ' ', 'ㅚ','ㅟ',' ',
          'ㄳ','ㄵ','ㄶ','ㄺ','ㄻ',  'ㄼ','ㄽ','ㄾ','ㄿ','ㅀ',
          ' ',' ',' ',' ',' ',       ' ',' ',' ',' ',    'ㅄ',
          ' ',' ',' ',' ',' ',       ' ',' ',' ',' ',' ',
          ' ',' ',' ',' ',' ',       ' ',' ',' ','영/특','확인'];
    Window_NameInput.LATIN =
        [ 'A','B','C','D','E',  'a','b','c','d','e',
          'F','G','H','I','J',  'f','g','h','i','j',
          'K','L','M','N','O',  'k','l','m','n','o',
          'P','Q','R','S','T',  'p','q','r','s','t',
          'U','V','W','X','Y',  'u','v','w','x','y',
          'Z','[',']','^','_',  'z','{','}','|','~',
          '0','1','2','3','4',  '!','#','$','%','&',
          '5','6','7','8','9',  '(',')','*','+','-',
          '/','=','@','<','>',  ':',';',' ','한','확인' ];
    Window_NameInput.prototype.initialize = function (editWindow) {
        var x = editWindow.x;
        var y = editWindow.y + editWindow.height + 8;
        var width = editWindow.width;
        var height = this.windowHeight();
        Window_Selectable.prototype.initialize.call(this, x, y, width, height);
        this._editWindow = editWindow;
        this._page = 0;
        this._index = 0;
        this.initialConsonantCode = -1;
        this.medialVowelCode = -1;
        this.finalConsonantCode = 0;
        this.hangul = '';
        this.finalConsonantChar = '';
        this.refresh();
        this.updateCursor();
        this.activate();
    };
    Window_NameInput.prototype.refresh = function () {
        var table = this.table();
        this.contents.clear();
        this.resetTextColor();
        this.clearHangul();
        for (var i = 0; i < 90; i++) {
            var rect = this.itemRect(i);
            rect.x += 3;
            rect.width -= 6;
            this.drawText(table[this._page][i], rect.x, rect.y, rect.width, 'center');
        }
    };
    Window_NameInput.prototype.processBack = function () {

        if (this._editWindow.back()) {
            if (this.finalConsonantCode != 0) {
                this.finalConsonantCode = 0;
                this.hangul = combineHangul(this.initialConsonantCode, this.medialVowelCode, 0);
                this._editWindow.add(this.hangul);
            }
            else if (this.medialVowelCode != -1) {
                this.medialVowelCode = -1;
                this.hangul = initialConsonantCharTable(this.initialConsonantCode);
                this._editWindow.add(this.hangul);
            }
            else if (this.initialConsonantCode != -1) {
                this.initialConsonantCode = -1;
            }
            SoundManager.playCancel();
        }
    };
    Window_NameInput.prototype.processCancel = function () {
        this.processBack();
    };
    Window_NameInput.prototype.processOk = function () {
        this.characterTemp = this.character()
        if (this.characterTemp) {
            this.onNameAdd();
        }
        else if (this.isPageChange()) {
            SoundManager.playOk();
            this.cursorPagedown();
        }
        else if (this.isOk()) {
            this.onNameOk();
        }
    };
    Window_NameInput.prototype.onNameAdd = function () {
        if (this._editWindow.add(this.characterTemp)) {
            SoundManager.playOk();
        }
        else {
            SoundManager.playBuzzer();
        }
    };
    Window_NameEdit.prototype.charWidth = function () {
        return this.textWidth('\uff21');
    };
    Window_NameInput.prototype.table = function () {
        switch (hangulType) {
            case 0:
                return [Window_NameInput.HANGUL1,
                        Window_NameInput.LATIN];
            case 1:
                return [Window_NameInput.HANGUL2,
                        Window_NameInput.LATIN];
        }
    };
    Window_NameInput.prototype.character = function () {
        if (this._page == 0) {
            return this.hangulCharacter();
        }
        else {
            return this._index < 88 ? this.table()[this._page][this._index] : '';
        }
    };
    Window_NameInput.prototype.hangulCharacter = function () {
        var currentChar = this._index < 88 ? this.table()[this._page][this._index] : '';
        return this.readHangul(currentChar, false);
    };
    Window_NameInput.prototype.readHangul = function (currentChar, isNextWord) {
        var result;
        // 초성이 저장되지 않았으면...
        if (this.initialConsonantCode == -1) {
            this.initialConsonantCode = initialConsonantCodeTable(currentChar);
            this.hangul = currentChar;
            result = this.hangul;
            isNextWord = true;
        }
            // 혹은 중성이 저장되지 않았으면...
        else if (this.medialVowelCode == -1) {
            this.medialVowelCode = medialVowelCodeTable(currentChar);
            if (this.medialVowelCode != -1) {
                this.hangul = combineHangul(this.initialConsonantCode, this.medialVowelCode, 0);
                result = this.hangul;
            }
            else {
                // 모두 지우고 다음 문자부터 새로 쓴다.
                isNextWord = true;
                this.clearHangul();
                result = this.readHangul(currentChar, false);
            }
        }
            // 혹은 종성이 저장되지 않았으면...
        else if (this.finalConsonantCode == 0) {
            this.finalConsonantCode = finalConsonantCodeTable(currentChar);
            if (this.finalConsonantCode != 0) {
                this.finalConsonantChar = currentChar;
                this.hangul = combineHangul(this.initialConsonantCode, this.medialVowelCode, this.finalConsonantCode);
                result = this.hangul;
            }
            else {
                // 모두 지우고 다음 문자부터 새로 쓴다.
                isNextWord = true;
                this.clearHangul();
                result = this.readHangul(currentChar, false);
            }
        }
            // 모든 문자가 저장되어있고 중성이 입력됬으면...
        else if (medialVowelCodeTable(currentChar) != -1) {
            var tokebibool = initialConsonantCodeTable(this.finalConsonantChar);
            // 종성이 초성이 될 수 있다면...
            if (tokebibool != -1) {
                // (도깨비불 현상)
                isNextWord = true;
                this._editWindow.back();
                this._editWindow.add(String.fromCharCode(((this.initialConsonantCode * 21) + this.medialVowelCode) * 28 + 0xac00));

                this.initialConsonantCode = tokebibool;
                this.medialVowelCode = medialVowelCodeTable(currentChar);
                this.finalConsonantCode = 0;
                this.hangul = combineHangul(this.initialConsonantCode, this.medialVowelCode, this.finalConsonantCode);
                result = this.hangul;
            }
        }
            // 모든 문자가 저장되어있으면...
        else {
            // 모두 지우고 다음 문자부터 새로 쓴다.
            isNextWord = true;
            this.clearHangul();
            result = this.readHangul(currentChar, false);
        }

        // isNextWord가 false이면 한칸 지운다.
        if (!isNextWord) {
            this._editWindow.back();
        }
        return result;
    };
    Window_NameInput.prototype.clearHangul = function () {
        this.initialConsonantCode = -1;
        this.medialVowelCode = -1;
        this.finalConsonantCode = 0;
        this.hangul = '';
    }
    function combineHangul(initialConsonantCode, medialVowelCode, finalConsonantCode) {
        return String.fromCharCode(((initialConsonantCode * 21) + medialVowelCode) * 28 + finalConsonantCode + 0xac00);
    }
    //==============================================
    // 테이블
    //==============================================
    function initialConsonantCharTable(code) {
        switch (code) {
            case 0:
                return 'ㄱ';
            case 1:
                return 'ㄲ';
            case 2:
                return 'ㄴ';
            case 3:
                return 'ㄷ';
            case 4:
                return 'ㄸ';
            case 5:
                return 'ㄹ';
            case 6:
                return 'ㅁ';
            case 7:
                return 'ㅂ';
            case 8:
                return 'ㅃ';
            case 9:
                return 'ㅅ';
            case 10:
                return 'ㅆ';
            case 11:
                return 'ㅇ';
            case 12:
                return 'ㅈ';
            case 13:
                return 'ㅉ';
            case 14:
                return 'ㅊ';
            case 15:
                return 'ㅋ';
            case 16:
                return 'ㅌ';
            case 17:
                return 'ㅍ';
            case 17:
                return 'ㅎ';
            default:
                return '';
        }
    }
    function initialConsonantCodeTable(char) {
        switch (char) {
            case 'ㄱ':
                return 0;
            case 'ㄲ':
                return 1;
            case 'ㄴ':
                return 2;
            case 'ㄷ':
                return 3;
            case 'ㄸ':
                return 4;
            case 'ㄹ':
                return 5;
            case 'ㅁ':
                return 6;
            case 'ㅂ':
                return 7;
            case 'ㅃ':
                return 8;
            case 'ㅅ':
                return 9;
            case 'ㅆ':
                return 10;
            case 'ㅇ':
                return 11;
            case 'ㅈ':
                return 12;
            case 'ㅉ':
                return 13;
            case 'ㅊ':
                return 14;
            case 'ㅋ':
                return 15;
            case 'ㅌ':
                return 16;
            case 'ㅍ':
                return 17;
            case 'ㅎ':
                return 18;
            default:
                return -1;
        }
    }
    function medialVowelCodeTable(char) {
        switch (char) {
            case 'ㅏ':
                return 0;
            case 'ㅐ':
                return 1;
            case 'ㅑ':
                return 2;
            case 'ㅒ':
                return 3;
            case 'ㅓ':
                return 4;
            case 'ㅔ':
                return 5;
            case 'ㅕ':
                return 6;
            case 'ㅖ':
                return 7;
            case 'ㅗ':
                return 8;
            case 'ㅘ':
                return 9;
            case 'ㅙ':
                return 10;
            case 'ㅚ':
                return 11;
            case 'ㅛ':
                return 12;
            case 'ㅜ':
                return 13;
            case 'ㅝ':
                return 14;
            case 'ㅞ':
                return 15;
            case 'ㅟ':
                return 16;
            case 'ㅠ':
                return 17;
            case 'ㅡ':
                return 18;
            case 'ㅢ':
                return 19;
            case 'ㅣ':
                return 20;
            default:
                return -1;
        }
    }
    function finalConsonantCodeTable(char) {
        switch (char) {
            case 'ㄱ':
                return 1;
            case 'ㄲ':
                return 2;
            case 'ㄳ':
                return 3;
            case 'ㄴ':
                return 4;
            case 'ㄵ':
                return 5;
            case 'ㄶ':
                return 6;
            case 'ㄷ':
                return 7;
            case 'ㄹ':
                return 8;
            case 'ㄺ':
                return 9;
            case 'ㄻ':
                return 10;
            case 'ㄼ':
                return 11;
            case 'ㄽ':
                return 12;
            case 'ㄾ':
                return 13;
            case 'ㄿ':
                return 14;
            case 'ㅀ':
                return 15;
            case 'ㅁ':
                return 16;
            case 'ㅂ':
                return 17;
            case 'ㅄ':
                return 18;
            case 'ㅅ':
                return 19;
            case 'ㅆ':
                return 20;
            case 'ㅇ':
                return 21;
            case 'ㅈ':
                return 22;
            case 'ㅊ':
                return 23;
            case 'ㅋ':
                return 24;
            case 'ㅌ':
                return 25;
            case 'ㅍ':
                return 26;
            case 'ㅎ':
                return 27;
            default:
                return 0;
        }
    }
})();