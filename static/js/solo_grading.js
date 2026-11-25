// SLOPBOWL SOLO GRADING MIRROR
// Pure normalization and answer matching for the solo drill. This is the browser
// counterpart of core/grading.py; solo.js keeps thin wrappers for its existing call sites.

const SoloGrading = (() => {
  // =========================================================
  // JUDGING / SCORING
  // =========================================================
  function soloNormalizeText(value) {
    return String(value || '')
      .toLowerCase()
      .replace(/[\u2018\u2019\u201c\u201d]/g, "'")
      .replace(/[^a-z0-9'\s]/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  // =========================================================
  // ANSWER GRADING (mirrors core/grading.py — keep the two in step)
  // =========================================================
  // A packet line may carry alternatives — "6,000 (ACCEPT: 6 x 10^3)" — and a line may list
  // its answers with a semicolon ("MACH; KNOTS"). Every listed form is equally correct, so
  // judging is candidate based: any one of them matching makes the response correct.
  // Parentheticals that are not ACCEPT groups are annotations — "(in any order)",
  // "(Solution: ...)" — and are never part of the answer.

  const SOLO_SUPERSCRIPTS = { '\u2070': '0', '\u00b9': '1', '\u00b2': '2', '\u00b3': '3', '\u2074': '4', '\u2075': '5', '\u2076': '6', '\u2077': '7', '\u2078': '8', '\u2079': '9', '\u207b': '-' };
  const SOLO_ACCEPT_GROUP = /\(\s*accept\s*:?\s*([^)]*)\)/gi;
  const SOLO_PARENTHETICAL = /\([^)]*\)/g;
  const SOLO_ACCEPT_PREFIX = /^\s*accept\s*:?\s*(.+)$/i;
  const SOLO_ALT_SPLIT = /\s*(?:;|\bor\b)\s*/i;
  // The letter must be followed by a bracket/period/colon or whitespace, so a short answer
  // that merely begins with a choice letter ("X-ray") is never read as option X.
  const SOLO_MC_LINE = /^\s*([WXYZ])\s*(?:[).:]\s*|\s+)(\S.*)$/i;
  // A reply that leads with a choice letter. The separator or end of input is required, so a
  // word that merely starts with a choice letter ("water") is never read as the letter W.
  const SOLO_MC_REPLY = /^\s*([wxyz])\s*(?:[).:\-]\s*(.*)|$)/i;
  const SOLO_PLAIN_NUMBER = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/;
  const SOLO_NUMBER_PATTERN = '([+-]?(?:\\d+(?:\\.\\d*)?|\\.\\d+))';
  const SOLO_SCIENTIFIC = new RegExp(`^${SOLO_NUMBER_PATTERN}\\s*(?:[x*\u00d7]|times)\\s*10\\s*(?:\\^|to the|to)?\\s*([+-]?\\d+)(?:st|nd|rd|th)?$`);
  const SOLO_SCIENTIFIC_SPOKEN = new RegExp(`^${SOLO_NUMBER_PATTERN}\\s*(?:[x*\u00d7]|times)\\s*10\\s*(?:to the\\s+)?(squared|cubed|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)$`);
  const SOLO_EXPONENT = new RegExp(`^${SOLO_NUMBER_PATTERN}[eE]([+-]?\\d+)$`);
  const SOLO_ORDINAL_POWERS = { squared: 2, cubed: 3, third: 3, fourth: 4, fifth: 5, sixth: 6, seventh: 7, eighth: 8, ninth: 9, tenth: 10 };
  const SOLO_NUMBER_WORDS = {
    zero: 0, one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9,
    ten: 10, eleven: 11, twelve: 12, thirteen: 13, fourteen: 14, fifteen: 15, sixteen: 16,
    seventeen: 17, eighteen: 18, nineteen: 19, twenty: 20, thirty: 30, forty: 40, fifty: 50,
    sixty: 60, seventy: 70, eighty: 80, ninety: 90, hundred: 100, thousand: 1000,
    million: 1e6, billion: 1e9, half: 0.5, quarter: 0.25,
  };
  // Units may trail a numeric answer: the NSB appendix does not require units to be stated.
  // Only these tokens — or words the packet's own answer line uses — may be dropped, so
  // "90 south" is never read as a bare 90 when the packet's answer says north.
  const SOLO_UNIT_WORDS = new Set(`
    m cm mm nm km meter meters metre metres centimeter centimeters centimetre centimetres
    millimeter millimeters millimetre millimetres micrometer micrometers micron microns
    nanometer nanometers angstrom angstroms inch inches in foot feet ft yard yards yd
    mile miles mi g gram grams gm kg kilogram kilograms kilogramme mg milligram milligrams
    pound pounds lb lbs ounce ounces oz ton tons tonne tonnes amu dalton daltons
    s sec secs second seconds minute minutes min hour hours hr hrs day days week weeks
    month months year years yr millisecond milliseconds microsecond microseconds
    nanosecond nanoseconds k kelvin kelvins celsius centigrade fahrenheit rankine
    degree degrees a amp ampere amperes amps v volt volts ohm ohms watt watts w joule
    joules j coulomb coulombs farad farads henry henries tesla teslas weber webers siemens
    hertz hz pascal pascals pa newton newtons n bar bars atmosphere atmospheres atm psi
    torr mmhg calorie calories cal kilocalorie kilocalories kcal electronvolt electronvolts
    ev kilojoule kilojoules kj megajoule megajoules mj mole moles mol molar candela lumen
    lumens lux l liter liters litre litres ml milliliter milliliters millilitre millilitres
    cc cubic gallon gallons quart quarts pint pints cup cups becquerel becquerels curie
    curies sievert sieverts gray grays rad rem percent percentage ppm
  `.split(/\s+/).filter(Boolean));
  // Words that carry no answer content: articles, prepositions, and the spoken preamble a
  // typed answer often opens with. They may surround a numeric answer, but a word that means
  // something ("south") never may.
  const SOLO_FILLER_WORDS = new Set(`
    a an the is are was were be of and or it its that this i my me answer answered think
    believe maybe probably approximately approx roughly around about so then there their
    in any order
  `.split(/\s+/).filter(Boolean));

  function soloNumbersEqual(first, second) {
    return Math.abs(first - second) <= Math.max(1e-9 * Math.max(Math.abs(first), Math.abs(second)), 1e-12);
  }

  function soloScaled(coefficient, exponent) {
    const value = Number(coefficient) * Math.pow(10, Number(exponent));
    return Number.isFinite(value) ? value : null;
  }

  // Handles the forms a science packet and a hurried typist actually produce: thousands
  // separators, scientific notation with any multiplication sign, superscript digits, and a
  // plain decimal written with or without a leading zero.
  function soloNumericValue(value) {
    let text = String(value == null ? '' : value).trim().toLowerCase();
    for (const [mark, digit] of Object.entries(SOLO_SUPERSCRIPTS)) text = text.split(mark).join(digit);
    text = text.replace(/[\u2018\u2019\u201c\u201d]/g, "'");
    while (/(\d),(\d)/.test(text)) text = text.replace(/(\d),(\d)/g, '$1$2');
    text = text.replace(/\s+/g, ' ').trim();
    if (!text) return null;
    let match = text.match(SOLO_SCIENTIFIC);
    if (match) return soloScaled(match[1], match[2]);
    match = text.match(SOLO_SCIENTIFIC_SPOKEN);
    if (match) return soloScaled(match[1], SOLO_ORDINAL_POWERS[match[2]]);
    match = text.replace(/\s+/g, '').match(SOLO_EXPONENT);
    if (match) return soloScaled(match[1], match[2]);
    const compact = text.replace(/\s+/g, '');
    if (SOLO_PLAIN_NUMBER.test(compact)) {
      const number = Number(compact);
      return Number.isFinite(number) ? number : null;
    }
    return null;
  }

  // A single spelled-out word ("zero", "half"); a compound such as "seven tenths" carries
  // its own meaning and is not guessed at, so multiple-choice wording stays exact.
  function soloSpelledNumberValue(value) {
    const text = soloNormalizeText(value);
    if (!text || text.includes(' ')) return null;
    return Object.prototype.hasOwnProperty.call(SOLO_NUMBER_WORDS, text) ? SOLO_NUMBER_WORDS[text] : null;
  }

  function soloAnswerValue(value) {
    const number = soloNumericValue(value);
    return number !== null ? number : soloSpelledNumberValue(value);
  }

  function soloIgnorableWord(word, allowedWords) {
    return SOLO_FILLER_WORDS.has(word) || SOLO_UNIT_WORDS.has(word) || allowedWords.has(word);
  }

  // Only a trailing unit word is dropped and never a trailing number, so a reply of
  // "6 x 10^3" stays the single number 6000 and is never reduced to a bare 6.
  function soloReplyNumber(value, allowedWords) {
    const text = String(value == null ? '' : value).replace(SOLO_PARENTHETICAL, ' ').replace(/\s+/g, ' ').trim();
    if (!text) return null;
    const whole = soloAnswerValue(text);
    if (whole !== null) return whole;
    let tokens = text.split(' ');
    while (tokens.length > 1) {
      if (soloAnswerValue(tokens[tokens.length - 1]) !== null) break;
      if (!soloIgnorableWord(soloNormalizeText(tokens[tokens.length - 1]), allowedWords)) break;
      tokens = tokens.slice(0, -1);
      const head = soloAnswerValue(tokens.join(' '));
      if (head !== null) return head;
    }
    return null;
  }

  // A numeric answer is decided by value, never by the digits appearing inside a longer
  // reply: "90 south" does not answer 90 NORTH, and "6 x 10^3" does not answer 6.
  function soloNumericReplyMatches(answerNumber, replyRaw, replyWords, allowedWords) {
    const replyNumber = soloReplyNumber(replyRaw, allowedWords);
    if (replyNumber !== null) return soloNumbersEqual(answerNumber, replyNumber);
    const numbers = replyWords.filter((word) => soloAnswerValue(word) !== null);
    if (numbers.length !== 1 || !soloNumbersEqual(answerNumber, soloAnswerValue(numbers[0]))) return false;
    return replyWords.filter((word) => soloAnswerValue(word) === null).every((word) => soloIgnorableWord(word, allowedWords));
  }

  // Bounded on purpose: a misspelling of a long word is a slip, but a short answer has too
  // few characters for a typo to stay unambiguous, so it must be spelled out.
  function soloTypoAllowance(word) {
    if (word.length < 6) return 0;
    if (word.length < 9) return 1;
    if (word.length < 15) return 2;
    return 3;
  }

  function soloIsPluralPair(first, second) {
    for (const [short, long] of [[first, second], [second, first]]) {
      if (long === short + 's' || long === short + 'es') return true;
      if (short.endsWith('y') && long === short.slice(0, -1) + 'ies') return true;
      if (short.endsWith('us') && long === short.slice(0, -2) + 'i') return true;
      if (short.endsWith('is') && long === short.slice(0, -2) + 'es') return true;
    }
    return false;
  }

  function soloEditDistanceWithin(first, second, limit) {
    if (Math.abs(first.length - second.length) > limit) return false;
    let previous = Array.from({ length: second.length + 1 }, (_, index) => index);
    for (let row = 1; row <= first.length; row += 1) {
      const current = [row];
      for (let column = 1; column <= second.length; column += 1) {
        current.push(Math.min(
          previous[column] + 1,
          current[column - 1] + 1,
          previous[column - 1] + (first[row - 1] === second[column - 1] ? 0 : 1),
        ));
      }
      if (Math.min(...current) > limit) return false;
      previous = current;
    }
    return previous[previous.length - 1] <= limit;
  }

  function soloWordsMatch(answerWord, replyWords) {
    if (replyWords.includes(answerWord)) return true;
    const allowance = soloTypoAllowance(answerWord);
    return replyWords.some((replyWord) => {
      if (soloIsPluralPair(answerWord, replyWord)) return true;
      return allowance > 0 && replyWord.length >= 5 && soloEditDistanceWithin(answerWord, replyWord, allowance);
    });
  }

  // Hyphen and space placement should not decide an answer. Equality only — never
  // containment — so "iron" cannot be found inside "environment".
  function soloCompactEqual(answer, reply, replyWords) {
    const compact = String(answer == null ? '' : answer).toLowerCase().replace(/[^a-z0-9]+/g, '');
    if (compact.length < 4) return false;
    const replyCompact = String(reply == null ? '' : reply).toLowerCase().replace(/[^a-z0-9]+/g, '');
    if (compact === replyCompact) return true;
    return replyWords.some((word) => String(word).toLowerCase().replace(/[^a-z0-9]+/g, '') === compact);
  }

  function soloMatchesCandidate(candidate, replyRaw, reply, replyWords, allowedWords) {
    const answer = soloNormalizeText(candidate);
    if (!answer) return false;
    if (reply === answer) return true;
    const answerNumber = soloNumericValue(candidate);
    if (answerNumber !== null) return soloNumericReplyMatches(answerNumber, replyRaw, replyWords, allowedWords);
    if (` ${reply} `.includes(` ${answer} `)) return true;
    if (soloCompactEqual(answer, reply, replyWords)) return true;
    const answerWords = answer.split(' ').filter(Boolean);
    if (answerWords.length === 1) return soloWordsMatch(answerWords[0], replyWords);
    const significant = answerWords.filter((word) => word.length >= 4);
    if (!significant.length) return answerWords.every((word) => soloWordsMatch(word, replyWords));
    return significant.every((word) => soloWordsMatch(word, replyWords));
  }

  function soloLooksNumeric(text) {
    return SOLO_PLAIN_NUMBER.test(String(text).replace(/\s+/g, ''));
  }

  // Commas are only separators when the pieces are not all plain numbers, so
  // "(ACCEPT: 1,000)" stays one answer while "(ACCEPT: RADII, RADIUS)" becomes two.
  function soloAlternatives(group) {
    const pieces = [];
    for (const piece of group.split(SOLO_ALT_SPLIT)) {
      const trimmed = piece.trim();
      if (!trimmed) continue;
      const commaParts = trimmed.split(',').map((part) => part.trim()).filter(Boolean);
      if (commaParts.length > 1 && !commaParts.every(soloLooksNumeric)) pieces.push(...commaParts);
      else pieces.push(trimmed);
    }
    return pieces;
  }

  function soloCandidateAnswers(answerText) {
    const raw = String(answerText == null ? '' : answerText);
    const candidates = [];
    let base = raw.replace(SOLO_PARENTHETICAL, ' ').trim();
    const prefixed = base.match(SOLO_ACCEPT_PREFIX);
    if (prefixed) base = prefixed[1].trim();
    for (const piece of base.split(';')) {
      const trimmed = piece.trim();
      if (trimmed && !candidates.includes(trimmed)) candidates.push(trimmed);
    }
    for (const group of raw.match(SOLO_ACCEPT_GROUP) || []) {
      const inner = group.replace(/^\(\s*accept\s*:?\s*/i, '').replace(/\)\s*$/, '');
      for (const alternative of soloAlternatives(inner)) {
        if (alternative && !candidates.includes(alternative)) candidates.push(alternative);
      }
    }
    return candidates;
  }

  function soloPrintedAnswer(answerText) {
    return soloNormalizeText(String(answerText == null ? '' : answerText).replace(SOLO_PARENTHETICAL, ' '));
  }

  function soloJudgeKeyword(answerText, response) {
    const replyRaw = String(response == null ? '' : response);
    const reply = soloNormalizeText(replyRaw);
    if (!reply) return false;
    // Typing the answer line exactly as the packet prints it is always correct, even when the
    // line lists several values or carries a solution.
    const printed = soloPrintedAnswer(answerText);
    if (printed && reply === printed) return true;
    const replyWords = reply.split(' ').filter(Boolean);
    const candidates = soloCandidateAnswers(answerText);
    // Words the packet itself uses are acceptable trailing qualifiers, so a question whose
    // answer line says "90 NORTH" still accepts "90 degrees north" and rejects "90 south".
    const allowedWords = new Set();
    for (const candidate of candidates) {
      for (const word of soloNormalizeText(candidate).split(' ')) if (word) allowedWords.add(word);
    }
    return candidates.some((candidate) => soloMatchesCandidate(candidate, replyRaw, reply, replyWords, allowedWords));
  }

  function soloChoiceLine(answerText) {
    const text = String(answerText == null ? '' : answerText).trim();
    const match = text.match(SOLO_MC_LINE);
    if (match) return [match[1].toLowerCase(), match[2].trim()];
    if (text.length === 1 && 'wxyz'.includes(text.toLowerCase())) return [text.toLowerCase(), ''];
    return [null, ''];
  }

  function soloReplyChoice(response) {
    const match = String(response == null ? '' : response).match(SOLO_MC_REPLY);
    if (!match) return [null, ''];
    return [match[1].toLowerCase(), (match[2] || '').trim()];
  }

  function soloPhrasesEqual(phrase, reply) {
    if (soloNormalizeText(phrase) === soloNormalizeText(reply)) return true;
    const phraseValue = soloAnswerValue(phrase);
    const replyValue = soloAnswerValue(reply);
    return phraseValue !== null && replyValue !== null && soloNumbersEqual(phraseValue, replyValue);
  }

  // Rule 3-3: the letter or the choice text; Rule 3-6: both, if both are given. The choice
  // text must match exactly once case and punctuation are folded.
  function soloJudgeMultipleChoice(answerText, response) {
    const candidates = soloCandidateAnswers(answerText);
    const base = candidates.length ? candidates[0] : '';
    const [letter, phrase] = soloChoiceLine(base);
    if (letter === null) return soloJudgeKeyword(answerText, response);
    const [replyLetter, replyPhrase] = soloReplyChoice(response);
    if (replyLetter === null) {
      return candidates.some((candidate) => {
        const text = soloChoiceLine(candidate)[1] || candidate;
        return Boolean(text) && soloPhrasesEqual(text, response);
      });
    }
    if (replyLetter !== letter) return false;
    if (replyPhrase && !soloPhrasesEqual(phrase, replyPhrase)) return false;
    return true;
  }

  // The entry point: alternatives, numeric equality, and the NSB multiple-choice rules.
  function soloJudgeAnswer(question, response) {
    const answerText = String((question || {}).tossup_answer || '');
    const candidates = soloCandidateAnswers(answerText);
    const isMultipleChoice = String((question || {}).type || '').trim().toUpperCase() === 'MC'
      || (candidates.length > 0 && soloChoiceLine(candidates[0])[0] !== null);
    if (isMultipleChoice) return soloJudgeMultipleChoice(answerText, response);
    return soloJudgeKeyword(answerText, response);
  }

  function soloJudgeBonus(question, response) {
    return soloJudgeAnswer(
      { ...question, tossup_answer: String(question.bonus_answer || '') },
      response,
    );
  }

  return {
    judgeAnswer: soloJudgeAnswer,
    judgeBonus: soloJudgeBonus,
    judgeKeyword: soloJudgeKeyword,
    judgeMultipleChoice: soloJudgeMultipleChoice,
  };
})();

window.SoloGrading = SoloGrading;
